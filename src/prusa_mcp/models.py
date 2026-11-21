import json
from dataclasses import dataclass

VALID_FILAMENT_STATUSES = {"sealed", "active", "low", "empty", "archived"}
VALID_PRINT_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class Filament:
    id: int
    brand: str
    material: str
    color: str
    color_hex: str | None
    weight_g: float
    remaining_g: float
    price: float | None
    currency: str
    purchased: str | None
    opened: str | None
    status: str
    notes: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "brand": self.brand,
            "material": self.material,
            "color": self.color,
            "color_hex": self.color_hex,
            "weight_g": self.weight_g,
            "remaining_g": self.remaining_g,
            "remaining_pct": round(self.remaining_g / self.weight_g * 100, 1)
            if self.weight_g > 0
            else 0,
            "price": self.price,
            "currency": self.currency,
            "purchased": self.purchased,
            "opened": self.opened,
            "status": self.status,
            "notes": self.notes,
        }


@dataclass
class PrintJob:
    id: int
    filename: str
    started_at: str
    finished_at: str | None
    duration_s: int | None
    filament_used_g: float | None
    filament_used_m: float | None
    filament_id: int | None
    status: str
    notes: str | None
    cost_breakdown_json: str | None
    quoted_price: float | None
    is_quote: bool
    created_at: str

    def to_dict(self) -> dict:
        duration_str = None
        if self.duration_s:
            hours, remainder = divmod(self.duration_s, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"

        return {
            "id": self.id,
            "filename": self.filename,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": duration_str,
            "duration_s": self.duration_s,
            "filament_used_g": self.filament_used_g,
            "filament_used_m": self.filament_used_m,
            "filament_id": self.filament_id,
            "status": self.status,
            "notes": self.notes,
            "cost_breakdown": (
                json.loads(self.cost_breakdown_json)
                if self.cost_breakdown_json
                else None
            ),
            "quoted_price": self.quoted_price,
            "is_quote": self.is_quote,
        }
