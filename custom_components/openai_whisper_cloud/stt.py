"""OpenAI Whisper API speech-to-text entity."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import io
import json
import wave

import requests

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_NAME,
    CONF_SOURCE,
    CONF_URL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _LOGGER
from .const import (
    CONF_CUSTOM_PROVIDER,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    SUPPORTED_LANGUAGES,
    BATCH_POLL_INTERVAL,
    BATCH_MAX_POLL_ATTEMPTS,
)
from .whisper_provider import WhisperModel, whisper_providers

# Mapping of regional language variants to base language codes for Whisper API.
# Whisper only supports base language codes (e.g., "zh"), not regional variants
# (e.g., "zh-tw"). These regional variants are needed for Home Assistant's
# intent recognition system to properly load language-specific intents.
# See: https://github.com/home-assistant/intents/issues/1104
LANGUAGE_TO_WHISPER: dict[str, str] = {
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hk": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whisper speech platform via config entry."""
    _LOGGER.debug(f"STT setup Entry {config_entry.entry_id}")

    async_add_entities([
        OpenAIWhisperCloudEntity(
            custom=config_entry.data.get(CONF_CUSTOM_PROVIDER, False),
            api_url=config_entry.data[CONF_URL] if config_entry.data.get(CONF_CUSTOM_PROVIDER) else whisper_providers[config_entry.data[CONF_SOURCE]].url,
            api_key=config_entry.data.get(CONF_API_KEY, ""),
            model= WhisperModel(config_entry.options[CONF_MODEL], SUPPORTED_LANGUAGES) if config_entry.data.get(CONF_CUSTOM_PROVIDER) else whisper_providers[config_entry.data[CONF_SOURCE]].models[config_entry.options[CONF_MODEL]],
            temperature=config_entry.options[CONF_TEMPERATURE],
            prompt=config_entry.options[CONF_PROMPT],
            name=config_entry.data[CONF_NAME],
            unique_id=config_entry.entry_id
        )
    ])



class OpenAIWhisperCloudEntity(SpeechToTextEntity):
    """OpenAI Whisper API provider entity."""

    def __init__(self, custom: bool, api_url: str, api_key: str, model: WhisperModel, temperature, prompt, name, unique_id) -> None:
        """Init STT service."""
        self.custom = custom
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.prompt = prompt
        self._attr_name = name
        self._attr_unique_id = unique_id

    @property
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""
        return self.model.languages

    @property
    def supported_formats(self) -> list[AudioFormats]:
        """Return a list of supported formats."""
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        """Return a list of supported codecs."""
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        """Return a list of supported bit rates."""
        return [
            AudioBitRates.BITRATE_8,
            AudioBitRates.BITRATE_16,
            AudioBitRates.BITRATE_24,
            AudioBitRates.BITRATE_32,
        ]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        """Return a list of supported sample rates."""
        return [
            AudioSampleRates.SAMPLERATE_8000,
            AudioSampleRates.SAMPLERATE_16000,
            AudioSampleRates.SAMPLERATE_44100,
            AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        """Return a list of supported channels."""
        return [AudioChannels.CHANNEL_MONO, AudioChannels.CHANNEL_STEREO]

    def _derive_result_url(self, post_url: str, batch_id: str) -> str:
        """Derive the Infomaniak batch result download URL from the POST URL."""
        # Infomaniak: POST /1/ai/{product_id}/openai/audio/transcriptions
        # Result:     GET  /1/ai/{product_id}/results/{batch_id}/download
        if "/openai/audio/transcriptions" in post_url:
            base = post_url.rsplit("/openai/audio/transcriptions", 1)[0]
            return f"{base}/results/{batch_id}/download"
        _LOGGER.warning(
            "Could not derive batch result URL from %s, using heuristic fallback",
            post_url,
        )
        # Best-effort fallback for non-standard custom URLs
        parts = post_url.rstrip("/").split("/")
        base = "/".join(parts[:-2]) if len(parts) > 2 else post_url
        return f"{base}/results/{batch_id}/download"

    async def _poll_batch_result(
        self, result_url: str, headers: dict
    ) -> str | None:
        """Poll the batch result URL until data is available or timeout."""
        for attempt in range(1, BATCH_MAX_POLL_ATTEMPTS + 1):
            try:
                response = await asyncio.to_thread(
                    requests.get,
                    result_url,
                    headers=headers,
                )
                _LOGGER.debug(
                    "Batch poll attempt %d/%d took %f s and returned %d - %s",
                    attempt,
                    BATCH_MAX_POLL_ATTEMPTS,
                    response.elapsed.seconds,
                    response.status_code,
                    response.reason,
                )

                if response.status_code == 200:
                    body = response.text
                    if body and body.strip():
                        _LOGGER.debug("Batch result body received")
                        return body.strip()
                    # Body is empty, not ready yet
                elif response.status_code == 404:
                    _LOGGER.debug("Batch result not ready (404), retrying...")
                else:
                    _LOGGER.warning(
                        "Unexpected status %d from batch result URL: %s",
                        response.status_code,
                        response.text,
                    )
            except requests.exceptions.RequestException as e:
                _LOGGER.warning("Batch poll request exception: %s", e)

            await asyncio.sleep(BATCH_POLL_INTERVAL)

        _LOGGER.error(
            "Batch transcription timed out after %d attempts",
            BATCH_MAX_POLL_ATTEMPTS,
        )
        return None

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Process an audio stream to STT service."""

        _LOGGER.debug("Processing audio stream: %s", metadata)

        data = b""
        async for chunk in stream:
            data += chunk
            if len(data) / (1024 * 1024) > 24.5:
                _LOGGER.error("Audio stream size exceed the maximum allowed by OpenAI which is 25Mb")
                return SpeechResult("", SpeechResultState.ERROR)

        if not data:
            _LOGGER.error("No audio data received")
            return SpeechResult("", SpeechResultState.ERROR)

        try:
            temp_file = io.BytesIO()
            with wave.open(temp_file, "wb") as wav_file:
                wav_file.setnchannels(metadata.channel)
                wav_file.setframerate(metadata.sample_rate)
                wav_file.setsampwidth(2)
                wav_file.writeframes(data)

            # Ensure the buffer is at the start before passing it
            temp_file.seek(0)

            _LOGGER.debug("Temp wav audio file created of %.2f Mb", temp_file.getbuffer().nbytes / (1024 * 1024))

            # Prepare the files parameter with a proper filename
            files = {
                "file": ("audio.wav", temp_file, "audio/wav"),
            }

            # Prepare the data payload
            # Convert regional language variants to base language for Whisper API
            whisper_language = LANGUAGE_TO_WHISPER.get(
                metadata.language.lower() if metadata.language else "",
                metadata.language,
            )
            if whisper_language != metadata.language:
                _LOGGER.debug(
                    "Converted language '%s' to '%s' for Whisper API",
                    metadata.language,
                    whisper_language,
                )
            data = {
                "model": self.model.name,
                "language": whisper_language,
                "temperature": self.temperature,
                "prompt": self.prompt,
                "response_format": "json",
            }

            # Make the request in a separate thread
            post_url = (
                f"{self.api_url}/v1/audio/transcriptions"
                if not self.custom
                else self.api_url
            )
            response = await asyncio.to_thread(
                requests.post,
                post_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                },
                files=files,
                data=data,
            )

            _LOGGER.debug(
                "Transcription request took %f s and returned %d - %s",
                response.elapsed.seconds,
                response.status_code,
                response.reason,
            )

            if response.status_code >= 400:
                _LOGGER.error(
                    "Transcription request failed with %d: %s",
                    response.status_code,
                    response.text,
                )
                return SpeechResult("", SpeechResultState.ERROR)

            try:
                result_json = response.json()
            except (json.JSONDecodeError, ValueError):
                _LOGGER.error("Failed to decode JSON response: %s", response.text)
                return SpeechResult("", SpeechResultState.ERROR)

            # Fast path: synchronous response with text
            if "text" in result_json:
                transcription = result_json.get("text", "")
                _LOGGER.debug("TRANSCRIPTION (sync): %s", transcription)
                if transcription:
                    return SpeechResult(transcription, SpeechResultState.SUCCESS)
                _LOGGER.error("Empty transcription in sync response")
                return SpeechResult("", SpeechResultState.ERROR)

            # Batch path: async response with batch_id
            batch_id = result_json.get("batch_id")
            if batch_id:
                _LOGGER.info("Batch ID received: %s", batch_id)
                result_url = self._derive_result_url(post_url, batch_id)
                _LOGGER.debug("Polling batch result at %s", result_url)

                result_body = await self._poll_batch_result(
                    result_url,
                    {"Authorization": f"Bearer {self.api_key}"},
                )
                if result_body is None:
                    return SpeechResult("", SpeechResultState.ERROR)

                try:
                    result_data = json.loads(result_body)
                    transcription = result_data.get("text", "")
                except (json.JSONDecodeError, AttributeError):
                    transcription = result_body

                _LOGGER.debug("TRANSCRIPTION (batch): %s", transcription)

                if transcription:
                    return SpeechResult(transcription, SpeechResultState.SUCCESS)

                _LOGGER.error("Empty transcription from batch result")
                return SpeechResult("", SpeechResultState.ERROR)

            _LOGGER.error("Unexpected response: %s", response.text)
            return SpeechResult("", SpeechResultState.ERROR)

        except requests.exceptions.RequestException as e:
            _LOGGER.error(e)
            return SpeechResult("", SpeechResultState.ERROR)
