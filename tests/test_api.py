def shares(stream):
    return {o["offer_id"]: (o["share"], o["state"]) for o in stream["offers"]}


def offer_stream(campaign):
    return next(s for s in campaign["streams"] if s["has_offers"])


# ---------- часть 1: создание


def test_create_campaign_two_streams(client, kt):
    r = client.post("/api/campaigns", json={"name": "c1", "geo": "mx, au", "offer_id": 2})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "test_khristoforov_c1"

    camp = next(c for c in kt.calls if c[0] == "create_campaign")[1]
    # домен, группа и источник подобраны по умолчанию через API
    assert (camp["domain_id"], camp["group_id"], camp["traffic_source_id"]) == (7, 3, 4)

    s1, s2 = [c[1] for c in kt.calls if c[0] == "create_stream"]
    assert s1["filters"] == [{"name": "country", "mode": "accept", "payload": ["MX", "AU"]}]
    assert s1["action_payload"] == "https://google.com" and s1["position"] == 1
    assert s2["offers"] == [{"offer_id": 2, "share": 100, "state": "active"}] and s2["position"] == 2

    # в редакторе кампания сразу видна: первый поток без офферов, второй — с оффером 100%
    streams = body["streams"]
    assert [s["has_offers"] for s in streams] == [False, True]
    assert shares(streams[1]) == {2: (100, "active")}
    assert streams[1]["dirty"] is False


def test_create_keeps_existing_prefix_and_explicit_ids(client, kt):
    r = client.post("/api/campaigns", json={"name": "test_khristoforov_x", "geo": "RO",
                                            "offer_id": 1, "domain_id": 8, "group_id": 3, "source_id": 4})
    assert r.json()["name"] == "test_khristoforov_x"
    assert kt.calls[0][1]["domain_id"] == 8


def test_create_validates_geo_and_offer(client):
    assert client.post("/api/campaigns", json={"name": "c", "geo": "Russia", "offer_id": 1}).status_code == 400
    assert client.post("/api/campaigns", json={"name": "c", "geo": "RU", "offer_id": 999}).status_code == 404


# ---------- часть 2: редактор (сценарий из видео)


def test_fetch_ignores_streams_without_offers(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    c = client.post(f"/api/campaigns/{cid}/fetch").json()
    assert [s["has_offers"] for s in c["streams"]] == [False, True]
    assert shares(offer_stream(c)) == {1: (34, "active"), 2: (33, "active"), 3: (33, "active")}


def test_add_offer_then_cancel(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")

    s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4}).json()
    assert [o["share"] for o in s["offers"]] == [25, 25, 25, 25]
    assert s["dirty"] is True
    assert len(kt.streams_db[sid]["offers"]) == 3  # в Keitaro ничего не поменялось

    s = client.post(f"/api/streams/{sid}/cancel").json()
    assert s["dirty"] is False
    assert shares(s) == {1: (34, "active"), 2: (33, "active"), 3: (33, "active")}


def test_add_then_push(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4})
    s = client.post(f"/api/streams/{sid}/push").json()
    assert s["dirty"] is False
    assert kt.streams_db[sid]["offers"] == [
        {"offer_id": i, "share": 25, "state": "active"} for i in (1, 2, 3, 4)
    ]


def test_remove_push_and_bring_back(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")

    s = client.delete(f"/api/streams/{sid}/offers/2").json()
    assert shares(s) == {1: (50, "active"), 2: (0, "removed"), 3: (50, "active")}
    assert s["dirty"] is True

    s = client.post(f"/api/streams/{sid}/push").json()
    assert s["dirty"] is False
    assert [o["offer_id"] for o in kt.streams_db[sid]["offers"]] == [1, 3]
    assert shares(s)[2] == (0, "removed")  # остался у нас в архиве

    s = client.post(f"/api/streams/{sid}/offers/2/restore").json()
    assert s["dirty"] is True
    assert shares(s) == {1: (34, "active"), 2: (33, "active"), 3: (33, "active")}
    client.post(f"/api/streams/{sid}/push")
    assert [o["offer_id"] for o in kt.streams_db[sid]["offers"]] == [1, 2, 3]


def test_offer_deleted_in_keitaro_goes_to_archive(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    # пользователь удалил два оффера прямо в Keitaro
    kt.streams_db[sid]["offers"] = [{"offer_id": 1, "share": 100, "state": "active"}]

    c = client.post(f"/api/campaigns/{cid}/fetch").json()
    s = offer_stream(c)
    assert shares(s) == {1: (100, "active"), 2: (0, "removed"), 3: (0, "removed")}
    assert s["dirty"] is False

    s = client.post(f"/api/streams/{sid}/offers/3/restore").json()
    assert shares(s)[3] == (50, "active")


def test_pin_survives_rebalance(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4})  # 25/25/25/25
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True})
    client.delete(f"/api/streams/{sid}/offers/3")
    s = client.post(f"/api/streams/{sid}/push").json()
    assert shares(s) == {1: (25, "active"), 2: (38, "active"), 3: (0, "removed"), 4: (37, "active")}

    # возвращаем удалённый — закреплённые 25% не трогаются
    s = client.post(f"/api/streams/{sid}/offers/3/restore").json()
    assert shares(s) == {1: (25, "active"), 2: (25, "active"), 3: (25, "active"), 4: (25, "active")}

    # закрепить с новым значением
    s = client.put(f"/api/streams/{sid}/offers/2/pin", json={"pinned": True, "share": 40}).json()
    assert shares(s) == {1: (25, "active"), 2: (40, "active"), 3: (18, "active"), 4: (17, "active")}
    assert sum(o["share"] for o in s["offers"]) == 100


def test_pin_over_100_rejected_and_state_kept(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True, "share": 80})
    r = client.put(f"/api/streams/{sid}/offers/2/pin", json={"pinned": True, "share": 30})
    assert r.status_code == 400
    s = client.get(f"/api/campaigns/{cid}").json()
    assert shares(offer_stream(s))[2] == (10, "active")


def test_cannot_add_same_offer_twice(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    assert client.post(f"/api/streams/{sid}/offers", json={"offer_id": 1}).status_code == 400


def test_offer_search(client):
    r = client.get("/api/offers", params={"q": "offer 3"}).json()
    assert r == [{"id": 3, "name": "Offer 3"}]
    assert client.get("/api/offers", params={"q": "5"}).json()[0]["id"] == 5


def test_unlisted_offer_needs_flag(client, kt, campaign_with_offers):
    from app.config import get_settings

    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    assert client.post(f"/api/streams/{sid}/offers", json={"offer_id": 777}).status_code == 404

    # Keitaro пускает в поток оффер, карточку которого аккаунт не видит — разрешаем только явно
    get_settings().allow_unlisted_offers = True
    s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": 777}).json()
    added = next(o for o in s["offers"] if o["offer_id"] == 777)
    assert "нет в списке" in added["name"] and added["share"] == 25
    assert client.post(f"/api/streams/{sid}/offers", json={"offer_id": 0}).status_code == 422
    # в автодополнение такие не попадают
    assert all(o["id"] != 777 for o in client.get("/api/offers").json())


# ---------- регрессии из ревью


def test_pinned_offer_deleted_in_keitaro_comes_back_unpinned(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.put(f"/api/streams/{sid}/offers/2/pin", json={"pinned": True, "share": 30})
    client.post(f"/api/streams/{sid}/push")
    kt.streams_db[sid]["offers"] = [{"offer_id": 1, "share": 50}, {"offer_id": 3, "share": 50}]
    client.post(f"/api/campaigns/{cid}/fetch")
    s = client.post(f"/api/streams/{sid}/offers/2/restore").json()
    assert shares(s) == {1: (34, "active"), 2: (33, "active"), 3: (33, "active")}
    assert not any(o["pinned"] for o in s["offers"])


def test_cancel_restores_pin(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True})
    client.post(f"/api/streams/{sid}/push")  # закреплено и опубликовано 34
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": False})
    client.delete(f"/api/streams/{sid}/offers/2")
    s = client.post(f"/api/streams/{sid}/cancel").json()
    assert {o["offer_id"]: o["pinned"] for o in s["offers"]} == {1: True, 2: False, 3: False}
    s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4}).json()
    assert shares(s)[1] == (34, "active")  # закрепление пережило Cancel


def test_remove_when_only_pinned_left(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True, "share": 30})
    client.delete(f"/api/streams/{sid}/offers/3")
    r = client.delete(f"/api/streams/{sid}/offers/2")
    assert r.status_code == 200
    assert shares(r.json())[1] == (30, "active")
    r = client.post(f"/api/streams/{sid}/push")
    assert r.status_code == 400 and "закреплены" in r.json()["detail"]


def test_unpublished_offer_is_not_archived(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4})
    s = client.delete(f"/api/streams/{sid}/offers/4").json()
    assert 4 not in shares(s) and s["dirty"] is False

    client.post(f"/api/streams/{sid}/offers", json={"offer_id": 5})
    c = client.post(f"/api/campaigns/{cid}/fetch").json()
    assert 5 not in shares(offer_stream(c))


def test_disabled_offer_survives_push(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    kt.streams_db[sid]["offers"].append({"offer_id": 4, "share": 10, "state": "disabled"})
    c = client.post(f"/api/campaigns/{cid}/fetch").json()
    assert shares(offer_stream(c))[4] == (10, "disabled")
    assert client.delete(f"/api/streams/{sid}/offers/4").status_code == 400
    client.delete(f"/api/streams/{sid}/offers/3")
    client.post(f"/api/streams/{sid}/push")
    assert kt.offers_in_stream(sid) == [(1, 50, "active"), (2, 50, "active"), (4, 10, "disabled")]


def test_push_refuses_empty_stream(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    for oid in (1, 2, 3):
        client.delete(f"/api/streams/{sid}/offers/{oid}")
    r = client.post(f"/api/streams/{sid}/push")
    assert r.status_code == 400
    assert len(kt.streams_db[sid]["offers"]) == 3


def test_push_detects_changes_made_in_keitaro(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4})
    kt.streams_db[sid]["offers"] = [{"offer_id": 1, "share": 100}]  # кто-то поправил в Keitaro
    r = client.post(f"/api/streams/{sid}/push")
    assert r.status_code == 409
    assert kt.offers_in_stream(sid) == [(1, 100, "active")]


def test_cannot_add_offer_to_redirect_stream(client, campaign_with_offers):
    cid, _ = campaign_with_offers
    c = client.post(f"/api/campaigns/{cid}/fetch").json()
    google = next(s for s in c["streams"] if not s["has_offers"])
    assert client.post(f"/api/streams/{google['id']}/offers", json={"offer_id": 4}).status_code == 400


def test_geo_must_be_iso(client):
    r = client.post("/api/campaigns", json={"name": "c", "geo": "UK", "offer_id": 1})
    assert r.status_code == 400 and "GB" in r.json()["detail"]


def test_missing_default_domain_is_an_error(client, kt):
    kt.domains = lambda: [{"id": 8, "name": "other.xyz"}]
    r = client.post("/api/campaigns", json={"name": "c", "geo": "AU", "offer_id": 1})
    assert r.status_code == 400 and not kt.campaigns_db
    # а с явно выбранным доменом — создаётся
    r = client.post("/api/campaigns", json={"name": "c", "geo": "AU", "offer_id": 1, "domain_id": 8})
    assert r.status_code == 201


def test_create_survives_failed_fetch(client, kt):
    def boom(cid):
        raise RuntimeError("network")
    kt.streams = boom
    r = client.post("/api/campaigns", json={"name": "c", "geo": "AU", "offer_id": 1})
    assert r.status_code == 201 and len(kt.campaigns_db) == 1


def test_offer_list_is_cached(client, kt):
    calls = []
    original = kt.offers
    kt.offers = lambda: calls.append(1) or original()
    for q in ("o", "of", "off"):
        client.get("/api/offers", params={"q": q})
    assert len(calls) == 1


# ---------- регрессии из второго прохода ревью


def test_pin_is_a_draft_change(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    s = client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True}).json()
    assert s["dirty"] is True  # закрепили на текущей доле — всё равно есть что отменить
    s = client.post(f"/api/streams/{sid}/cancel").json()
    assert not any(o["pinned"] for o in s["offers"]) and s["dirty"] is False


def test_fetch_drops_draft_pin(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.put(f"/api/streams/{sid}/offers/1/pin", json={"pinned": True, "share": 70})
    client.post(f"/api/campaigns/{cid}/fetch")
    s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4}).json()
    assert [o["share"] for o in s["offers"]] == [25, 25, 25, 25]


def test_fetch_rebalances_stream_with_wrong_total(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    kt.streams_db[sid]["offers"] = [{"offer_id": 1, "share": 34}, {"offer_id": 2, "share": 33}]
    s = offer_stream(client.post(f"/api/campaigns/{cid}/fetch").json())
    assert s["dirty"] is True and s["total"] == 100
    client.post(f"/api/streams/{sid}/push")
    assert kt.offers_in_stream(sid) == [(1, 50, "active"), (2, 50, "active")]


def test_disabled_share_not_counted(client, kt, campaign_with_offers):
    cid, sid = campaign_with_offers
    kt.streams_db[sid]["offers"] = [{"offer_id": 1, "share": 50}, {"offer_id": 2, "share": 50, "state": "disabled"},
                                    {"offer_id": 3, "share": 50}]
    client.post(f"/api/campaigns/{cid}/fetch")
    s = client.post(f"/api/streams/{sid}/offers", json={"offer_id": 4}).json()
    assert s["total"] == 100
    client.post(f"/api/streams/{sid}/push")
    active = [x for x in kt.offers_in_stream(sid) if x[2] == "active"]
    assert sum(x[1] for x in active) == 100
    assert (2, 50, "disabled") in kt.offers_in_stream(sid)


def test_offer_deleted_in_keitaro_cannot_be_added(client, kt, campaign_with_offers):
    from app import services

    cid, sid = campaign_with_offers
    client.post(f"/api/campaigns/{cid}/fetch")
    client.get("/api/offers")  # оффер 5 попал в кэш
    kt.offers_db = [o for o in kt.offers_db if o["id"] != 5]
    services._offers_loaded_at = 0.0  # кэш устарел
    assert client.post(f"/api/streams/{sid}/offers", json={"offer_id": 5}).status_code == 404
    assert all(o["id"] != 5 for o in client.get("/api/offers").json())


def test_offer_search_by_id_prefix(client):
    assert [o["id"] for o in client.get("/api/offers", params={"q": "3"}).json()] == [3]


# ---------- безопасность


def test_mutations_require_csrf_header(client, campaign_with_offers):
    cid, sid = campaign_with_offers
    r = client.post(f"/api/streams/{sid}/cancel", headers={"X-Requested-With": ""})
    assert r.status_code == 403
    assert client.get("/api/offers", headers={"X-Requested-With": ""}).status_code == 200


def test_foreign_host_rejected(client):
    assert client.get("/api/offers", headers={"Host": "evil.example"}).status_code == 400


def test_input_limits(client):
    assert client.post("/api/campaigns", json={"name": "x" * 300, "geo": "AU", "offer_id": 1}).status_code == 422
    assert client.post(f"/api/streams/{2**70}/push").status_code == 422


def test_page_has_csp(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
