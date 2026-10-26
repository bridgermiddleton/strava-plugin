import json
import os
import time

import pytest

import strava_api as A


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    """Scripted responses; records calls."""

    def __init__(self):
        self.posts = []
        self.gets = []
        self.get_queue = []
        self.post_queue = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data)))
        return self.post_queue.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, dict(params), dict(headers)))
        return self.get_queue.pop(0)


@pytest.fixture
def store(tmp_path):
    return A.TokenStore(tmp_path / "token.json")


def test_token_store_roundtrip_and_legacy(store):
    assert store.load() == {}
    key = store.upsert({"refresh_token": "r1", "athlete": {"id": 42, "firstname": "A"}})
    assert key == "42"
    assert store.load()["42"]["refresh_token"] == "r1"
    store.upsert({"athlete": {"id": 42}, "access_token": "x"})   # merge keeps refresh_token
    assert store.load()["42"]["refresh_token"] == "r1"
    mode = os.stat(store.path).st_mode & 0o777
    assert mode & 0o660 == 0o660
    # legacy single-entry layout
    store.path.write_text(json.dumps({"refresh_token": "z", "athlete": {"id": 7}}))
    assert list(store.load()) == ["7"]
    store.path.write_text("not json")
    assert store.load() == {}


def test_refresh_rotates_and_persists(store):
    store.upsert({"refresh_token": "old", "access_token": "stale", "expires_at": time.time() - 10,
                  "athlete": {"id": 1}})
    sess = FakeSession()
    sess.post_queue.append(FakeResp(200, {"access_token": "new", "refresh_token": "rot", "expires_at": time.time() + 21600}))
    client = A.StravaClient(store, "cid", "sec", session=sess)
    assert client.access_token_for("1") == "new"
    saved = store.load()["1"]
    assert saved["refresh_token"] == "rot"
    assert sess.posts[0][1]["grant_type"] == "refresh_token"
    # second call: still valid, no network
    assert client.access_token_for("1") == "new"
    assert len(sess.posts) == 1


def test_per_athlete_credentials_override(store):
    store.upsert({"refresh_token": "o", "athlete": {"id": 2}, "client_id": "mine", "client_secret": "s2"})
    sess = FakeSession()
    sess.post_queue.append(FakeResp(200, {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}))
    A.StravaClient(store, "global", "gs", session=sess).access_token_for("2")
    assert sess.posts[0][1]["client_id"] == "mine"


def test_missing_credentials_is_auth_error(store):
    store.upsert({"refresh_token": "o", "athlete": {"id": 3}})
    with pytest.raises(A.StravaAuthError):
        A.StravaClient(store, "", "", session=FakeSession()).access_token_for("3")
    with pytest.raises(A.StravaAuthError):
        A.StravaClient(store, "a", "b", session=FakeSession()).access_token_for("nope")


def test_fetch_activities_pages_and_rate_limit(store):
    store.upsert({"refresh_token": "o", "access_token": "ok", "expires_at": time.time() + 9999, "athlete": {"id": 4}})
    sess = FakeSession()
    page1 = [{"id": i} for i in range(A.PAGE_SIZE)]
    sess.get_queue += [FakeResp(200, page1, {"X-ReadRateLimit-Usage": "5,50", "X-ReadRateLimit-Limit": "100,1000"}),
                       FakeResp(200, [{"id": "last"}])]
    client = A.StravaClient(store, "c", "s", session=sess)
    acts = client.fetch_activities("4", 0, 10)
    assert len(acts) == A.PAGE_SIZE + 1
    assert sess.gets[1][1]["page"] == 2
    assert sess.gets[0][2]["Authorization"] == "Bearer ok"
    assert client.last_usage == "5,50 of 100,1000"
    sess.get_queue.append(FakeResp(429, {}))
    with pytest.raises(A.StravaRateLimited):
        client.fetch_activities("4", 0, 10)
    assert client.rate_limited_until > time.time()
    with pytest.raises(A.StravaRateLimited):      # backs off without a request
        client.fetch_activities("4", 0, 10)
    assert len(sess.gets) == 3


def test_fetch_401_is_auth_error(store):
    store.upsert({"refresh_token": "o", "access_token": "ok", "expires_at": time.time() + 9999, "athlete": {"id": 5}})
    sess = FakeSession()
    sess.get_queue.append(FakeResp(401, {"message": "Authorization Error"}))
    with pytest.raises(A.StravaAuthError):
        A.StravaClient(store, "c", "s", session=sess).fetch_activities("5", 0, 1)


def test_build_authorize_url():
    url = A.build_authorize_url("123")
    assert url.startswith(A.STRAVA_AUTHORIZE_URL + "?")
    assert "client_id=123" in url and "activity%3Aread_all" in url and "approval_prompt=force" in url


def test_exchange_code_errors():
    sess = FakeSession()
    sess.post_queue.append(FakeResp(400, {"message": "Bad Request"}))
    with pytest.raises(A.StravaAuthError):
        A.exchange_code("c", "s", "code", session=sess)
    sess.post_queue.append(FakeResp(200, {"access_token": "a", "athlete": {"id": 9}}))
    assert A.exchange_code("c", "s", "code", session=sess)["athlete"]["id"] == 9
