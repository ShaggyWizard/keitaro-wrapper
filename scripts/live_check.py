"""Проверка обоих сценариев на настоящем Keitaro (нужен KEITARO_API_KEY в .env).

    python -m scripts.live_check            # создаст кампанию test_khristoforov_live_<время>
    python -m scripts.live_check --geo AU   # другое гео

Ходит через HTTP API обёртки (как фронт), а результат каждого шага сверяет
напрямую с Keitaro. Локальная база — временная SQLite, рабочую не трогает.
Кампания остаётся в Keitaro, чтобы её можно было открыть в админке.
"""
import argparse
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.keitaro import get_keitaro
from app.main import app


def keitaro_offers(kt, stream_id):
    stream = next(s for s in kt.streams(cid) if s["id"] == stream_id)
    return {o["offer_id"]: o["share"] for o in stream.get("offers") or [] if o.get("state", "active") == "active"}


def step(title, ok, detail=""):
    print(("OK  " if ok else "FAIL") + f"  {title}" + (f"  — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(1)


parser = argparse.ArgumentParser()
parser.add_argument("--geo", default="AU")
parser.add_argument("--extra-offers", default="",
                    help="ID офферов через запятую, если GET /offers отдаёт меньше трёх "
                         "(нужен ALLOW_UNLISTED_OFFERS=true)")
args = parser.parse_args()

db_file = Path(tempfile.mkdtemp()) / "live.db"
engine = create_engine(f"sqlite:///{db_file}")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)


def _db():
    with Session() as s:
        yield s


app.dependency_overrides[get_db] = _db
client = TestClient(app, base_url="http://127.0.0.1", headers={"X-Requested-With": "keitaro-wrapper"})
kt = get_keitaro()

ids = [o["id"] for o in sorted(kt.offers(), key=lambda o: o["id"])]
step("в Keitaro виден хотя бы один оффер", bool(ids), f"видно офферов: {len(ids)}")
ids += [int(x) for x in args.extra_offers.split(",") if x and int(x) not in ids]
step("для редактора есть три оффера", len(ids) >= 3, "передайте --extra-offers 111,222")
o1, o2, o3 = ids[:3]

# ---------- часть 1
name = f"live_{time.strftime('%Y%m%d_%H%M%S')}"
r = client.post("/api/campaigns", json={"name": name, "geo": args.geo, "offer_id": o1})
step("кампания создана", r.status_code == 201, r.text[:300])
c = r.json()
cid = c["id"]
print(f"      {c['name']} (id {cid})")

info = kt.campaign(cid)
meta = client.get("/api/meta").json()["defaults"]
step("домен, группа, источник выставлены",
     (info.get("domain_id"), int(info.get("group_id") or 0), info.get("traffic_source_id"))
     == (meta["domain_id"], meta["group_id"], meta["source_id"]),
     f"domain={info.get('domain_id')} group={info.get('group_id')} source={info.get('traffic_source_id')}")

streams = sorted(kt.streams(cid), key=lambda s: s["position"])
step("два потока", len(streams) == 2)
s1, s2 = streams
f = (s1.get("filters") or [{}])[0]
step("поток 1: страна → Google",
     f.get("name") == "country" and f.get("payload") == [args.geo] and "google" in str(s1.get("action_payload")),
     f"filter={f} action={s1.get('action_type')}:{s1.get('action_payload')}")
step("поток 2: оффер 100%", keitaro_offers(kt, s2["id"]) == {o1: 100}, str(keitaro_offers(kt, s2["id"])))

# ---------- часть 2
sid = s2["id"]
client.post(f"/api/campaigns/{cid}/fetch")

s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": o2}).json()
step("добавили оффер — 50/50, поток не опубликован", s["dirty"] and [o["share"] for o in s["offers"]] == [50, 50])
step("в Keitaro пока по-старому", keitaro_offers(kt, sid) == {o1: 100})
client.post(f"/api/streams/{sid}/cancel")
step("Cancel вернул как было", keitaro_offers(kt, sid) == {o1: 100}
     and len(client.get(f"/api/campaigns/{cid}").json()["streams"][1]["offers"]) == 1)

client.post(f"/api/streams/{sid}/offers", json={"offer_id": o2})
client.post(f"/api/streams/{sid}/offers", json={"offer_id": o3})
client.post(f"/api/streams/{sid}/push")
step("Push: три оффера 34/33/33", keitaro_offers(kt, sid) == {o1: 34, o2: 33, o3: 33}, str(keitaro_offers(kt, sid)))

client.put(f"/api/streams/{sid}/offers/{o1}/pin", json={"pinned": True, "share": 50})
client.delete(f"/api/streams/{sid}/offers/{o3}")
client.post(f"/api/streams/{sid}/push")
step("Pin 50% + Remove: 50/50, удалённый остался в архиве",
     keitaro_offers(kt, sid) == {o1: 50, o2: 50}
     and {o["offer_id"]: o["state"] for o in client.get(f"/api/campaigns/{cid}").json()["streams"][1]["offers"]}[o3] == "removed",
     str(keitaro_offers(kt, sid)))

client.post(f"/api/streams/{sid}/offers/{o3}/restore")
client.post(f"/api/streams/{sid}/push")
step("Bring back при закреплённых 50%: 50/25/25", keitaro_offers(kt, sid) == {o1: 50, o2: 25, o3: 25}, str(keitaro_offers(kt, sid)))

# удаляем оффер прямо в Keitaro, как в видео
kt.update_stream(sid, {"offers": [{"offer_id": o1, "share": 50, "state": "active"},
                                  {"offer_id": o2, "share": 50, "state": "active"}]})
c = client.post(f"/api/campaigns/{cid}/fetch").json()
states = {o["offer_id"]: o["state"] for o in c["streams"][1]["offers"]}
step("удалённый в Keitaro оффер ушёл в архив", states == {o1: "active", o2: "active", o3: "removed"}, str(states))

after = next(s for s in kt.streams(cid) if s["id"] == sid)
step("Push не трогает остальные поля потока",
     (after["schema"], after["type"], after["position"], after["state"]) == ("landings", "regular", 2, "active"))

# чужая правка в Keitaro между Fetch и Push не затирается
client.post(f"/api/streams/{sid}/offers/{o3}/restore")
kt.update_stream(sid, {"offers": [{"offer_id": o1, "share": 100, "state": "active"}]})
r = client.post(f"/api/streams/{sid}/push")
step("Push после правки в Keitaro — отказ 409, Keitaro не тронут",
     r.status_code == 409 and keitaro_offers(kt, sid) == {o1: 100}, f"{r.status_code} {keitaro_offers(kt, sid)}")

print(f"\nГотово. Кампания {c['name']} (id {cid}) осталась в Keitaro.")
