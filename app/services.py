import logging
import re
import secrets
import string
import time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.countries import ISO_COUNTRIES
from app.keitaro import KeitaroClient
from app.models import ACTIVE, DISABLED, REMOVED, Campaign, Offer, Stream, StreamOffer
from app.weights import Slot, rebalance

log = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------- общие помощники


def parse_geo(geo: str) -> list[str]:
    codes = [c.strip().upper() for c in re.split(r"[,\s;]+", geo) if c.strip()]
    bad = [c for c in codes if c not in ISO_COUNTRIES]
    if not codes or bad:
        hint = " (Великобритания — GB, не UK)" if "UK" in bad else ""
        raise ServiceError(
            f"Гео — коды стран ISO через запятую, например AU или MX,RO. Неверно: {', '.join(bad) or geo!r}{hint}"
        )
    return list(dict.fromkeys(codes))


def with_prefix(name: str) -> str:
    prefix = get_settings().campaign_prefix
    name = name.strip()
    if not name:
        raise ServiceError("Название кампании пустое")
    return name if name.startswith(prefix) else prefix + name


def _match(items: list[dict], wanted: str) -> dict | None:
    def norm(v: str) -> str:
        return str(v).strip().lower().rstrip("/").split("://")[-1]

    return next((item for item in items if norm(item.get("name", "")) == norm(wanted)), None)


def meta(kt: KeitaroClient) -> dict:
    """Справочники для формы создания + id значений по умолчанию (None, если в Keitaro такого нет)."""
    s = get_settings()
    domains, groups, sources = kt.domains(), kt.groups(), kt.traffic_sources()

    def pick(items, wanted):
        found = _match(items, wanted)
        return found["id"] if found else None

    return {
        "prefix": s.campaign_prefix,
        "domains": [{"id": d["id"], "name": d["name"]} for d in domains],
        "groups": [{"id": g["id"], "name": g["name"]} for g in groups],
        "sources": [{"id": t["id"], "name": t["name"]} for t in sources],
        "defaults": {
            "domain_id": pick(domains, s.default_domain),
            "group_id": pick(groups, s.default_group),
            "source_id": pick(sources, s.default_source),
        },
    }


# ---------- кэш офферов

_offers_loaded_at = 0.0
_listed_ids: set[int] = set()  # ID из последнего GET /offers — удалённые в Keitaro сюда не попадут
UNLISTED = "нет в списке офферов Keitaro"


def _upsert_offer(db: Session, offer_id: int, name: str, overwrite: bool = True) -> None:
    offer = db.get(Offer, offer_id)
    if offer is None:
        db.add(Offer(id=offer_id, name=name))
    elif overwrite:
        offer.name = name


def refresh_offers(db: Session, kt: KeitaroClient, force: bool = False) -> None:
    """Подтянуть офферы из Keitaro в локальную таблицу. Не чаще раза в offers_ttl секунд,
    чтобы автодополнение не скачивало весь список на каждую букву."""
    global _offers_loaded_at, _listed_ids
    if not force and time.monotonic() - _offers_loaded_at < get_settings().offers_ttl:
        return
    remote = kt.offers()
    _listed_ids = {o["id"] for o in remote}
    for o in remote:
        _upsert_offer(db, o["id"], o["name"])
    try:
        db.commit()
    except IntegrityError:
        # параллельный запрос уже вставил те же офферы — это не ошибка
        db.rollback()
    _offers_loaded_at = time.monotonic()


def search_offers(db: Session, kt: KeitaroClient, q: str, limit: int = 20) -> list[Offer]:
    refresh_offers(db, kt)
    q = q.strip().lower()
    offers = [o for o in db.query(Offer).order_by(Offer.id).all() if o.id in _listed_ids]
    if q:
        offers = [o for o in offers if q in o.name.lower() or str(o.id).startswith(q)]
    return offers[:limit]


def _ensure_offer(db: Session, kt: KeitaroClient, offer_id: int) -> Offer:
    """Оффер должен быть в GET /offers. Исключение — ALLOW_UNLISTED_OFFERS=true:
    Keitaro принимает в поток любой ID (на тестовом стенде аккаунту виден один оффер),
    тогда такой оффер разрешён, но помечается в названии."""
    if offer_id <= 0:
        raise ServiceError("ID оффера должен быть положительным числом")
    refresh_offers(db, kt)
    if offer_id not in _listed_ids:
        refresh_offers(db, kt, force=True)  # вдруг оффер только что создали
    offer = db.get(Offer, offer_id)
    if offer_id not in _listed_ids:
        if not get_settings().allow_unlisted_offers:
            raise ServiceError(f"Оффер {offer_id} не найден в Keitaro", 404)
        if offer is None:
            offer = Offer(id=offer_id, name=f"Offer {offer_id} ({UNLISTED})")
            db.add(offer)
            db.flush()
    return offer


# ---------- часть 1: создаватор кампаний


def _alias() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(7))


def create_campaign(
    db: Session,
    kt: KeitaroClient,
    *,
    name: str,
    geo: str,
    offer_id: int,
    domain_id: int | None = None,
    group_id: int | None = None,
    source_id: int | None = None,
) -> Campaign:
    countries = parse_geo(geo)
    full_name = with_prefix(name)
    _ensure_offer(db, kt, offer_id)

    if domain_id is None or group_id is None or source_id is None:
        defaults = meta(kt)["defaults"]
        domain_id = domain_id or defaults["domain_id"]
        group_id = group_id or defaults["group_id"]
        source_id = source_id or defaults["source_id"]
    if not (domain_id and group_id and source_id):
        raise ServiceError("Не найдены домен, группа или источник по умолчанию — выберите их в форме")

    campaign = kt.create_campaign(
        {
            "name": full_name,
            "alias": _alias(),
            "type": "position",
            "state": "active",
            "domain_id": domain_id,
            "group_id": group_id,
            "traffic_source_id": source_id,
            "cost_type": "CPC",
            "cost_value": 0,
            "cost_currency": "USD",
            "cost_auto": True,
        }
    )
    cid = campaign["id"]
    try:
        # Поток 1: все из выбранных стран -> Google
        kt.create_stream(
            {
                "campaign_id": cid,
                "name": "Geo " + ",".join(countries) + " -> Google",
                "type": "regular",
                "position": 1,
                "state": "active",
                "schema": "redirect",
                "action_type": "http",
                "action_payload": get_settings().google_url,
                "filters": [{"name": "country", "mode": "accept", "payload": countries}],
            }
        )
        # Поток 2: остальные -> оффер
        kt.create_stream(
            {
                "campaign_id": cid,
                "name": "Offers",
                "type": "regular",
                "position": 2,
                "state": "active",
                "schema": "landings",
                "action_type": "http",
                "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
            }
        )
    except Exception:
        # Не оставляем в Keitaro полусобранную кампанию
        try:
            kt.delete_campaign(cid)
        except Exception:
            log.exception("Не удалось удалить полусобранную кампанию %s", cid)
        raise

    db.merge(Campaign(
        id=cid, name=campaign.get("name", full_name), alias=campaign.get("alias"),
        geo=",".join(countries), created_by_wrapper=True,
    ))
    db.commit()
    # Кампания уже создана: если загрузка потоков сорвётся, не отдаём ошибку,
    # иначе пользователь нажмёт Create ещё раз и получит дубль.
    try:
        fetch_streams(db, kt, cid)
    except Exception:
        db.rollback()
        log.exception("Кампания %s создана, но потоки не загрузились", cid)
    return db.get(Campaign, cid)


# ---------- часть 2: редактор


def get_campaign(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ServiceError("Кампания ещё не загружена — нажмите Fetch streams", 404)
    return campaign


def _remote_snapshot(remote_offers: list[dict]) -> dict[int, tuple[int, str]]:
    """{offer_id: (share, state)} — то, что реально лежит в потоке Keitaro."""
    return {
        o["offer_id"]: (int(o.get("share") or 0), ACTIVE if o.get("state", "active") == "active" else DISABLED)
        for o in remote_offers or []
    }


def _published_snapshot(stream: Stream) -> dict[int, tuple[int, str]]:
    return {
        so.offer_id: (so.published_share or 0, so.published_state)
        for so in stream.offers
        if so.published_state in (ACTIVE, DISABLED)
    }


def fetch_streams(db: Session, kt: KeitaroClient, campaign_id: int) -> Campaign:
    """Синхронизация с Keitaro. Keitaro — источник правды, неопубликованный черновик сбрасывается:
    - офферы из Keitaro становятся опубликованными (выключенные — DISABLED);
    - опубликованные раньше офферы, которых в Keitaro больше нет, уходят в архив (removed);
    - привязки, которые ни разу не публиковались, удаляются;
    - потоки без офферов сохраняем просто как поток."""
    info = kt.campaign(campaign_id)
    campaign = db.get(Campaign, campaign_id) or Campaign(id=campaign_id, name=info["name"])
    campaign.name = info["name"]
    campaign.alias = info.get("alias")
    db.add(campaign)

    remote_streams = kt.streams(campaign_id)
    remote_ids = {s["id"] for s in remote_streams}
    for stream in list(campaign.streams):
        if stream.id not in remote_ids:
            db.delete(stream)

    offer_names: dict[int, str] | None = None
    for rs in remote_streams:
        stream = db.get(Stream, rs["id"]) or Stream(id=rs["id"], campaign_id=campaign_id)
        stream.name = rs.get("name") or f"Stream {rs['id']}"
        stream.position = rs.get("position") or 0
        stream.schema = rs.get("schema")
        db.add(stream)

        remote = _remote_snapshot(rs.get("offers"))
        for offer_id in remote:
            if db.get(Offer, offer_id) is None:
                if offer_names is None:
                    offer_names = {o["id"]: o["name"] for o in kt.offers()}
                _upsert_offer(db, offer_id, offer_names.get(offer_id, f"Offer {offer_id} ({UNLISTED})"))
        db.flush()

        known = {so.offer_id: so for so in stream.offers}
        for offer_id, (share, state) in remote.items():
            so = known.get(offer_id)
            if so is None:
                so = StreamOffer(stream_id=stream.id, offer_id=offer_id, pinned=False)
                stream.offers.append(so)
            so.share = so.published_share = share
            so.state = so.published_state = state
            if state != ACTIVE:
                so.published_pinned = False
            so.pinned = so.published_pinned  # черновое закрепление сбрасывается вместе с черновиком
        for offer_id, so in known.items():
            if offer_id in remote:
                continue
            if so.published_state is None:
                stream.offers.remove(so)  # черновик, которого в Keitaro никогда не было
                continue
            so.share = so.published_share = 0
            so.state = so.published_state = REMOVED
            so.pinned = so.published_pinned = False
        stream.has_offers = bool(stream.offers) or stream.schema == "landings"
        # В Keitaro сумма активных долей может быть не 100 (например, оффер удалили там руками).
        # Сразу пересчитываем черновик: поток станет «не опубликован», и Push исправит Keitaro.
        active = [so for so in stream.offers if so.state == ACTIVE]
        if active and sum(so.share for so in active) != 100:
            _rebalance(stream)
    db.commit()
    db.refresh(campaign)
    return campaign


def _stream(db: Session, stream_id: int) -> Stream:
    stream = db.get(Stream, stream_id)
    if stream is None:
        raise ServiceError("Поток не найден", 404)
    return stream


def _rebalance(stream: Stream) -> None:
    slots = [Slot(so.share, so.state == ACTIVE, so.pinned, frozen=so.state == DISABLED) for so in stream.offers]
    for so, share in zip(stream.offers, rebalance(slots)):
        so.share = share


def _binding(stream: Stream, offer_id: int) -> StreamOffer:
    for so in stream.offers:
        if so.offer_id == offer_id:
            return so
    raise ServiceError("Оффер не привязан к потоку", 404)


def add_offer(db: Session, kt: KeitaroClient, stream_id: int, offer_id: int) -> Stream:
    stream = _stream(db, stream_id)
    if stream.schema and stream.schema != "landings":
        raise ServiceError("В этот поток нельзя добавить оффер: он не отправляет на офферы")
    _ensure_offer(db, kt, offer_id)
    existing = next((so for so in stream.offers if so.offer_id == offer_id), None)
    if existing and existing.state == ACTIVE:
        raise ServiceError("Этот оффер уже есть в потоке")
    if existing and existing.state == DISABLED:
        raise ServiceError("Оффер выключен в самом Keitaro — включите его там и нажмите Fetch streams")
    if existing:
        existing.state = ACTIVE  # был в архиве — возвращаем
        existing.pinned = False
    else:
        stream.offers.append(StreamOffer(offer_id=offer_id, share=0, state=ACTIVE))
    stream.has_offers = True
    _rebalance(stream)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ServiceError("Этот оффер уже добавляется в поток")
    return stream


def remove_offer(db: Session, stream_id: int, offer_id: int) -> Stream:
    stream = _stream(db, stream_id)
    so = _binding(stream, offer_id)
    if so.state == DISABLED:
        raise ServiceError("Оффер выключен в самом Keitaro — им управляют там")
    if so.published_state is None:
        stream.offers.remove(so)  # ещё не публиковался — архив ему не нужен
    else:
        so.state = REMOVED
        so.pinned = False
    _rebalance(stream)
    db.commit()
    return stream


def bring_back(db: Session, stream_id: int, offer_id: int) -> Stream:
    stream = _stream(db, stream_id)
    so = _binding(stream, offer_id)
    if so.state != REMOVED:
        raise ServiceError("Вернуть можно только оффер из архива")
    so.state = ACTIVE
    so.pinned = False
    _rebalance(stream)
    db.commit()
    return stream


def set_pin(db: Session, stream_id: int, offer_id: int, pinned: bool, share: int | None = None) -> Stream:
    stream = _stream(db, stream_id)
    so = _binding(stream, offer_id)
    if so.state != ACTIVE:
        raise ServiceError("Закрепить можно только активный оффер")
    if share is not None and not 0 <= share <= 100:
        raise ServiceError("Доля — целое число от 0 до 100")
    new_share = share if (pinned and share is not None) else so.share
    others = sum(o.share for o in stream.offers if o is not so and o.state == ACTIVE and o.pinned)
    if pinned and others + new_share > 100:
        raise ServiceError(f"Закреплённые доли дадут {others + new_share}% — больше 100%")
    so.pinned = pinned
    so.share = new_share
    _rebalance(stream)
    db.commit()
    return stream


def cancel(db: Session, stream_id: int) -> Stream:
    """Откат к опубликованному: новые привязки удаляются, остальные возвращают долю, статус и закрепление."""
    stream = _stream(db, stream_id)
    for so in list(stream.offers):
        if so.published_state is None:
            stream.offers.remove(so)
        else:
            so.share = so.published_share or 0
            so.state = so.published_state
            so.pinned = so.published_pinned
    db.commit()
    return stream


def push(db: Session, kt: KeitaroClient, stream_id: int) -> Stream:
    stream = _stream(db, stream_id)
    active = [so for so in stream.offers if so.state == ACTIVE]
    if not active:
        raise ServiceError("После публикации в потоке не останется ни одного оффера — верните или добавьте оффер")
    total = sum(so.share for so in active)
    if total != 100:
        hint = " Все офферы закреплены — снимите закрепление с одного из них." if all(so.pinned for so in active) else ""
        raise ServiceError(f"Сумма долей {total}%, должна быть 100%.{hint}")

    # Защита от затирания: если поток в Keitaro поменяли после нашего Fetch, не перезаписываем вслепую
    remote = next((s for s in kt.streams(stream.campaign_id) if s["id"] == stream.id), None)
    if remote is None:
        raise ServiceError("Поток удалён в Keitaro — нажмите Fetch streams", 409)
    if _remote_snapshot(remote.get("offers")) != _published_snapshot(stream):
        raise ServiceError("Поток изменили в Keitaro после загрузки — нажмите Fetch streams и повторите", 409)

    payload = [
        {"offer_id": so.offer_id, "share": so.share, "state": "active" if so.state == ACTIVE else "disabled"}
        for so in stream.offers
        if so.state in (ACTIVE, DISABLED)  # выключенные в Keitaro отправляем как есть, чтобы не потерять
    ]
    kt.update_stream(stream.id, {"offers": payload})
    for so in stream.offers:
        so.published_share = so.share
        so.published_state = so.state
        so.published_pinned = so.pinned
    db.commit()
    return stream
