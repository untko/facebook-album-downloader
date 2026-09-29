from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class Photo:
    index: int
    facebook_url: str = ""
    direct_url: str = ""
    filename: str = ""
    downloaded: bool = False
    media_id: str = ""
    extracted_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "Photo":
        return cls(
            index=int(data.get("index") or index),
            facebook_url=str(data.get("facebook_url") or data.get("fb_url") or ""),
            direct_url=str(data.get("direct_url") or data.get("url") or data.get("img_url") or ""),
            filename=str(data.get("filename") or ""),
            downloaded=bool(data.get("downloaded", False)),
            media_id=str(data.get("media_id") or ""),
            extracted_at=str(data.get("extracted_at") or ""),
        )
