import copy
import os
import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.keitaro import KeitaroError, get_keitaro
from app.main import app


class FakeKeitaro:
    """Keitaro в памяти: хранит кампании и потоки так же, как их отдаёт Admin API."""

    def __init__(self):
        self.ids = itertools.count(100)
        self.offers_db = [{"id": i, "name": f"Offer {i}"} for i in (1, 2, 3, 4, 5)]
        self.campaigns_db: dict[int, dict] = {}
        self.streams_db: dict[int, dict] = {}
        self.calls: list[tuple] = []

    def offers(self): return copy.deepcopy(self.offers_db)
    def domains(self): return [{"id": 7, "name": "https://campaigns.example/"}, {"id": 8, "name": "other.xyz"}]
    def groups(self): return [{"id": 3, "name": "FORTESTS"}]
    def traffic_sources(self): return [{"id": 4, "name": "FORTESTS"}]
    def campaigns(self): return [{"id": c["id"], "name": c["name"]} for c in self.campaigns_db.values()]

    def campaign(self, cid):
        if cid not in self.campaigns_db:
            raise KeitaroError(404, "Not found")
        return copy.deepcopy(self.campaigns_db[cid])

    def create_campaign(self, payload):
        self.calls.append(("create_campaign", payload))
        c = {"id": next(self.ids), **payload}
        self.campaigns_db[c["id"]] = c
        return copy.deepcopy(c)

    def delete_campaign(self, cid):
        self.campaigns_db.pop(cid, None)

    def streams(self, cid):
        return copy.deepcopy([s for s in self.streams_db.values() if s["campaign_id"] == cid])

    def create_stream(self, payload):
        self.calls.append(("create_stream", payload))
        s = {"id": next(self.ids), "offers": [], "filters": [], **copy.deepcopy(payload)}
        for i, o in enumerate(s["offers"]):
            o.setdefault("id", 1000 + i)
        self.streams_db[s["id"]] = s
        return copy.deepcopy(s)

    def offers_in_stream(self, sid):
        return [(o["offer_id"], o["share"], o.get("state", "active")) for o in self.streams_db[sid]["offers"]]

    def update_stream(self, sid, payload):
        self.calls.append(("update_stream", sid, payload))
        self.streams_db[sid].update(copy.deepcopy(payload))
        return copy.deepcopy(self.streams_db[sid])


@pytest.fixture
def kt():
    return FakeKeitaro()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    """Каждый тест со своими настройками и пустым кэшем офферов."""
    from app import services
    from app.config import get_settings

    monkeypatch.setattr(services, "_offers_loaded_at", 0.0)
    monkeypatch.setattr(get_settings(), "allow_unlisted_offers", False)
    monkeypatch.setattr(get_settings(), "offers_ttl", 60)
    monkeypatch.setattr(get_settings(), "default_domain", "campaigns.example")


@pytest.fixture
def client(kt):
    url = os.environ.get("TEST_DATABASE_URL")  # например, PostgreSQL; по умолчанию SQLite в памяти
    if url:
        engine = create_engine(url)
        Base.metadata.drop_all(engine)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _db():
        with Session() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_keitaro] = lambda: kt
    yield TestClient(app, base_url="http://127.0.0.1", headers={"X-Requested-With": "keitaro-wrapper"})
    app.dependency_overrides.clear()


@pytest.fixture
def campaign_with_offers(kt):
    """Кампания как в видео: поток с гео -> Google и поток с тремя офферами по ~33%."""
    c = kt.create_campaign({"name": "test_khristoforov_video", "alias": "abc"})
    kt.create_stream({"campaign_id": c["id"], "name": "AU -> Google", "position": 1,
                      "schema": "redirect", "filters": [{"name": "country", "payload": ["AU"]}]})
    s = kt.create_stream({"campaign_id": c["id"], "name": "Offers", "position": 2, "schema": "landings",
                          "offers": [{"offer_id": 1, "share": 34}, {"offer_id": 2, "share": 33},
                                     {"offer_id": 3, "share": 33}]})
    return c["id"], s["id"]
