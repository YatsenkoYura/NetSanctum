"""
Platform Provider Strategy Architecture for Video Archiver.
Clean Registry pattern replacing cascades of if/elif checks for multi-platform support.
"""

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar
from urllib.parse import urlparse


class BasePlatformProvider(ABC):
    platform_id = "unknown"
    name = "Unknown"
    domains: tuple[str, ...] = ()
    extractor_keywords: tuple[str, ...] = ()
    key_cookies: tuple[str, ...] = ()

    @classmethod
    def matches(cls, url: str = "", extractor_name: str | None = None) -> bool:
        if extractor_name:
            ext = extractor_name.lower()
            if any(kw in ext for kw in cls.extractor_keywords):
                return True
        if url:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme in {"http", "https"} and any(
                hostname == domain or hostname.endswith(f".{domain}") for domain in cls.domains
            ):
                return True
        return False

    def validate_cookies(self, cookies_text: str | None) -> dict[str, Any]:
        """Validate format, expiration timestamp, and platform key cookies."""
        if not cookies_text or not cookies_text.strip():
            return {"has_cookies": False, "is_valid": False, "status": "missing", "message": "Куки не заданы"}

        lines = [
            line.strip()
            for line in cookies_text.strip().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            return {
                "has_cookies": False,
                "is_valid": False,
                "status": "invalid_format",
                "message": "Пустые куки",
            }

        now = int(time.time())
        found_cookie_names = set()
        expired_count = 0
        total_parsed = 0

        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 6:
                # Netscape format: domain, flag, path, secure, expiration, name, value
                exp_str = parts[4].strip()
                cookie_name = parts[5].strip()
                found_cookie_names.add(cookie_name)
                total_parsed += 1

                if exp_str.isdigit():
                    exp = int(exp_str)
                    if exp > 0 and exp < now:
                        expired_count += 1
            elif "=" in line:
                cookie_name = line.split("=", 1)[0].strip()
                found_cookie_names.add(cookie_name)
                total_parsed += 1

        # Expired check
        if total_parsed > 0 and expired_count == total_parsed:
            return {
                "has_cookies": True,
                "is_valid": False,
                "status": "expired",
                "message": "Срок действия куки истёк",
            }

        # Key cookies check
        if self.key_cookies:
            missing_keys = [k for k in self.key_cookies if k not in found_cookie_names]
            if missing_keys:
                really_missing = [k for k in missing_keys if k not in cookies_text]
                if really_missing:
                    return {
                        "has_cookies": True,
                        "is_valid": False,
                        "status": "missing_key_cookies",
                        "message": f"Истёк авторизационный токен ({', '.join(really_missing)})",
                    }

        if total_parsed > 0 and expired_count > 0:
            return {
                "has_cookies": True,
                "is_valid": True,
                "status": "partially_expired",
                "message": "Часть куки устарела",
            }

        return {"has_cookies": True, "is_valid": True, "status": "valid", "message": "Куки актуальны"}

    @abstractmethod
    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        """Return platform-specific yt-dlp parameters."""
        pass

    @abstractmethod
    def build_video_url(self, video_id: str) -> str:
        """Reconstruct the original URL from a video ID."""
        pass


class YouTubeProvider(BasePlatformProvider):
    platform_id = "youtube"
    name = "YouTube"
    domains = ("youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com")
    extractor_keywords = ("youtube",)
    key_cookies = ("SID", "HSID", "SSID", "LOGIN_INFO", "SAPISID", "__Secure-3PSIDTS")

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {
            "cachedir": "/app/storage/.cache/yt-dlp",
            "mark_watched": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["web", "mweb", "android", "ios"],
                }
            },
        }
        if custom_opts:
            # Merge extractor_args if provided
            if "extractor_args" in custom_opts and "youtube" in custom_opts["extractor_args"]:
                opts["extractor_args"]["youtube"].update(custom_opts["extractor_args"]["youtube"])
            opts.update({k: v for k, v in custom_opts.items() if k != "extractor_args"})
        return opts

    def build_video_url(self, video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"


class VKProvider(BasePlatformProvider):
    platform_id = "vk"
    name = "VK Video"
    domains = ("vk.com", "vkvideo.ru", "vk.ru", "m.vk.com", "m.vkvideo.ru")
    extractor_keywords = ("vk", "vkontakte")
    key_cookies = ("remixsid",)

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {
            "extractor_args": {
                "vk": {
                    "download_hd": ["True"],
                }
            }
        }
        if custom_opts:
            opts.update(custom_opts)
        return opts

    def build_video_url(self, video_id: str) -> str:
        return f"https://vk.com/video{video_id}"


class TelegramProvider(BasePlatformProvider):
    platform_id = "telegram"
    name = "Telegram"
    domains = ("t.me", "telegram.org", "telegram.me")
    extractor_keywords = ("telegram",)
    key_cookies = ("stel_token",)

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {
            "socket_timeout": 30,
        }
        if custom_opts:
            opts.update(custom_opts)
        return opts

    def build_video_url(self, video_id: str) -> str:
        return f"https://t.me/{video_id}"


class BoostyProvider(BasePlatformProvider):
    platform_id = "boosty"
    name = "Boosty"
    domains = ("boosty.to", "www.boosty.to")
    extractor_keywords = ("boosty",)
    key_cookies = ("auth", "boosty.sid")

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {}
        if custom_opts:
            opts.update(custom_opts)
        return opts

    def build_video_url(self, video_id: str) -> str:
        return f"https://boosty.to/{video_id}"


class RutubeProvider(BasePlatformProvider):
    platform_id = "rutube"
    name = "Rutube"
    domains = ("rutube.ru", "www.rutube.ru")
    extractor_keywords = ("rutube",)
    key_cookies = ("rutube_session",)

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {}
        if custom_opts:
            opts.update(custom_opts)
        return opts

    def build_video_url(self, video_id: str) -> str:
        return f"https://rutube.ru/video/{video_id}/"


class GenericProvider(BasePlatformProvider):
    platform_id = "other"
    name = "Direct / Other"
    domains = ()
    extractor_keywords = ()

    def get_ydl_opts(self, custom_opts: dict | None = None) -> dict[str, Any]:
        opts = {}
        if custom_opts:
            opts.update(custom_opts)
        return opts

    def build_video_url(self, video_id: str) -> str:
        return video_id


class PlatformRegistry:
    """Registry mapping URLs and yt-dlp extractors to platform strategy objects."""

    _providers: ClassVar[list[type[BasePlatformProvider]]] = [
        YouTubeProvider,
        VKProvider,
        TelegramProvider,
        BoostyProvider,
        RutubeProvider,
    ]

    @classmethod
    def get_provider(cls, url: str = "", extractor_name: str | None = None) -> BasePlatformProvider:
        for provider_cls in cls._providers:
            if provider_cls.matches(url, extractor_name):
                return provider_cls()
        return GenericProvider()

    @classmethod
    def require_supported_url(cls, url: str) -> BasePlatformProvider:
        provider = cls.get_provider(url)
        if isinstance(provider, GenericProvider):
            raise ValueError("Only supported video platform URLs are allowed")
        return provider

    @classmethod
    def get_provider_by_id(cls, platform_id: str) -> BasePlatformProvider:
        for provider_cls in cls._providers:
            if provider_cls.platform_id == platform_id:
                return provider_cls()
        return GenericProvider()

    @classmethod
    def detect_platform(cls, url: str = "", extractor_name: str | None = None) -> str:
        return cls.get_provider(url, extractor_name).platform_id

    @classmethod
    def list_platforms(cls) -> list[dict[str, str]]:
        result = [{"id": p.platform_id, "name": p.name} for p in cls._providers]
        result.append({"id": GenericProvider.platform_id, "name": GenericProvider.name})
        return result
