from contextlib import asynccontextmanager
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.config import get_settings
from app.db import get_db
from app.keitaro import KeitaroClient, KeitaroError, get_keitaro
from app.models import Campaign, Stream

STATIC = FsPath(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if get_keitaro.cache_info().currsize:
        get_keitaro().close()


app = FastAPI(title="Keitaro wrapper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "keitaro-wrapper"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def protect(request: Request, call_next):
    """Авторизации нет — обёртка рассчитана на запуск на 127.0.0.1. Поэтому закрываем два пути извне:
    - DNS-rebinding: запросы с чужим Host отклоняются;
    - CSRF: изменяющие запросы обязаны нести свой заголовок. Чужой сайт не может его поставить
      без CORS-preflight, а CORS обёртка не разрешает."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in get_settings().allowed_hosts:
        return JSONResponse({"detail": "Недопустимый Host"}, status_code=400)
    if (
        request.method not in SAFE_METHODS
        and request.url.path.startswith("/api/")
        and request.headers.get(CSRF_HEADER) != CSRF_VALUE
    ):
        return JSONResponse({"detail": f"Нет заголовка {CSRF_HEADER}"}, status_code=403)
    response = await call_next(request)
    # страница не использует inline-скрипты и внешние ресурсы — запрещаем всё остальное
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(services.ServiceError)
def service_error(_: Request, exc: services.ServiceError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(KeitaroError)
def keitaro_error(_: Request, exc: KeitaroError):
    status = exc.status if exc.status in (500, 504) else 502
    return JSONResponse({"detail": f"Keitaro: {exc.message} (код {exc.status})"}, status_code=status)


# ---------- схемы


MAX_ID = 2**31 - 1
IdPath = Annotated[int, Path(gt=0, le=MAX_ID)]


class CreateCampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    geo: str = Field(max_length=300, description="Коды стран через запятую: AU или MX,AU,RO")
    offer_id: int = Field(gt=0, le=MAX_ID)
    domain_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    group_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    source_id: int | None = Field(default=None, gt=0, le=MAX_ID)


class OfferIn(BaseModel):
    offer_id: int = Field(gt=0, le=MAX_ID)


class PinIn(BaseModel):
    pinned: bool
    share: int | None = Field(default=None, ge=0, le=100)


def stream_out(stream: Stream) -> dict:
    return {
        "id": stream.id,
        "name": stream.name,
        "position": stream.position,
        "has_offers": stream.has_offers,
        "schema": stream.schema,
        "dirty": stream.dirty,
        "total": sum(so.share for so in stream.offers if so.state == "active"),
        "offers": [
            {
                "offer_id": so.offer_id,
                "name": so.offer.name,
                "share": so.share,
                "state": so.state,
                "pinned": so.pinned,
                "published_share": so.published_share,
                "published_state": so.published_state,
                "dirty": so.dirty,
            }
            for so in stream.offers
        ],
    }


def campaign_out(c: Campaign) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "alias": c.alias,
        "geo": c.geo,
        "streams": [stream_out(s) for s in c.streams],
    }


# ---------- страницы и справочники


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def get_meta(kt: KeitaroClient = Depends(get_keitaro)):
    return services.meta(kt)


@app.get("/api/offers")
def get_offers(q: str = Query("", max_length=100), db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    return [{"id": o.id, "name": o.name} for o in services.search_offers(db, kt, q)]


@app.get("/api/campaigns")
def get_campaigns(kt: KeitaroClient = Depends(get_keitaro)):
    """Кампании из Keitaro (для выбора в редакторе)."""
    return [{"id": c["id"], "name": c["name"]} for c in kt.campaigns()]


# ---------- часть 1


@app.post("/api/campaigns", status_code=201)
def create_campaign(body: CreateCampaignIn, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    c = services.create_campaign(db, kt, **body.model_dump())
    return campaign_out(c)


# ---------- часть 2


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: IdPath, db: Session = Depends(get_db)):
    return campaign_out(services.get_campaign(db, campaign_id))


@app.post("/api/campaigns/{campaign_id}/fetch")
def fetch(campaign_id: IdPath, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    return campaign_out(services.fetch_streams(db, kt, campaign_id))


@app.post("/api/streams/{stream_id}/offers")
def add_offer(stream_id: IdPath, body: OfferIn, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    return stream_out(services.add_offer(db, kt, stream_id, body.offer_id))


@app.delete("/api/streams/{stream_id}/offers/{offer_id}")
def remove_offer(stream_id: IdPath, offer_id: IdPath, db: Session = Depends(get_db)):
    return stream_out(services.remove_offer(db, stream_id, offer_id))


@app.post("/api/streams/{stream_id}/offers/{offer_id}/restore")
def bring_back(stream_id: IdPath, offer_id: IdPath, db: Session = Depends(get_db)):
    return stream_out(services.bring_back(db, stream_id, offer_id))


@app.put("/api/streams/{stream_id}/offers/{offer_id}/pin")
def pin(stream_id: IdPath, offer_id: IdPath, body: PinIn, db: Session = Depends(get_db)):
    return stream_out(services.set_pin(db, stream_id, offer_id, body.pinned, body.share))


@app.post("/api/streams/{stream_id}/push")
def push(stream_id: IdPath, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    return stream_out(services.push(db, kt, stream_id))


@app.post("/api/streams/{stream_id}/cancel")
def cancel(stream_id: IdPath, db: Session = Depends(get_db)):
    return stream_out(services.cancel(db, stream_id))
