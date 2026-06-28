from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class Listing(BaseModel):
    """Annonce normalisee provenant d'un site source."""

    source: str
    source_id: str
    url: HttpUrl
    fetched_at: datetime

    title: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None

    asking_price: float | None = None
    municipal_evaluation: float | None = None
    annual_gross_revenue: float | None = None
    annual_expenses: float | None = None
    units: int | None = None
    year_built: int | None = None
    lot_area_sqft: float | None = None
    building_area_sqft: float | None = None

    description: str | None = None
    raw_html_path: str | None = None

    lat: float | None = None
    lon: float | None = None

    # Coûts annuels declarés / estimés
    municipal_tax: float | None = None       # $/an
    school_tax: float | None = None          # $/an

    # Évaluation séparée (le total est dans `municipal_evaluation`)
    eval_land: float | None = None
    eval_building: float | None = None

    # Méta annonce
    date_posted: datetime | None = None

    # Caractéristiques libres (chauffage, parking, etc.) — extension flexible.
    characteristics: dict[str, str] = Field(default_factory=dict)

    # ── Champs extraits par LLM depuis la description ──
    per_unit_rents: list[float] = Field(default_factory=list)        # loyers mensuels par logement
    per_unit_sizes: list[str] = Field(default_factory=list)          # "3 1/2", "4 1/2", etc.
    renovations_done: list[str] = Field(default_factory=list)
    renovations_needed: list[str] = Field(default_factory=list)
    units_occupied: int | None = None
    vacant_unit_notes: list[str] = Field(default_factory=list)
    seller_motivation: str | None = None
    rent_reset_potential: str | None = None                          # low/medium/high/unknown
    extraction_confidence: float | None = None
    extracted_at: datetime | None = None


class Metrics(BaseModel):
    mrb: float | None = None
    price_per_door: float | None = None
    price_to_eval: float | None = None
    estimated_noi: float | None = None
    estimated_cashflow_per_door_month: float | None = None


class RentComp(BaseModel):
    """Comparable de loyer — un logement a louer pour estimer le marche."""

    source: str
    source_id: str
    url: HttpUrl
    fetched_at: datetime

    title: str | None = None
    description: str | None = None
    city: str | None = None
    region: str | None = None
    address: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None

    monthly_rent: float | None = None
    size_label: str | None = None        # ex: "4 1/2", "studio"
    bedrooms: int | None = None          # derive de size_label si possible
    area_sqft: float | None = None
    heat_included: bool | None = None

    # Caractéristiques flexibles: parking, date dispo, animaux, terrasse, etc.
    characteristics: dict[str, str] = Field(default_factory=dict)


class ScreenVerdict(BaseModel):
    listing_source_id: str
    passes: bool
    score: float                         # 0..1, sur les checks evaluables
    checks: dict[str, bool | None]       # None = donnee manquante
    reasons: list[str]
    metrics: Metrics
