from docs.examples.middleware.rate_limit import app
from freezegun import freeze_time

from litestar.status_codes import HTTP_200_OK, HTTP_429_TOO_MANY_REQUESTS
from litestar.testing import TestClient


def test_rate_limit_middleware_example() -> None:
    with freeze_time() as frozen_time, TestClient(app=app) as client:
        # test "second" limit
        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert response.text == "ok"

        response = client.get("/")
        assert response.status_code == HTTP_429_TOO_MANY_REQUESTS

        # test "minute" limit
        frozen_time.tick(10)
        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert response.text == "ok"

        frozen_time.tick(10)
        response = client.get("/")
        assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
