import requests
from typing import Dict, Any, Union
from ..resources import SUCCESSFUL


DEFAULT_TIMEOUT = (5, 20)


class ApiHandler:
    def __init__(self, BASE: str):
        self.BASE = BASE.rstrip("/")

    async def request(self, endpoint: str, method: str = 'GET', image: bool = False, html: bool = False, **kwargs: Any) -> Union[Dict[str, Any], str, int, bytes]:
        url = self.BASE + endpoint
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)

        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException:
            return 500

        status_code: int = response.status_code

        if status_code != SUCCESSFUL:
            return status_code

        if image:
            return response.content

        if html:
            return response.text

        try:
            return response.json()
        except ValueError:
            return 500

    async def get(self, endpoint: str, *, params: Dict[str, Any] | None = None, **kwargs: Any) -> Union[Dict[str, Any], str, int, bytes]:
        return await self.request(endpoint, params=params or {}, method='GET', **kwargs)

    async def post(self, endpoint: str, *, data: Dict[str, Any] | None = None, **kwargs: Any) -> Union[Dict[str, Any], str, int, bytes]:
        return await self.request(endpoint, data=data or {}, method='POST', **kwargs)

    async def put(self, endpoint: str, *, data: Dict[str, Any] | None = None, **kwargs: Any) -> Union[Dict[str, Any], str, int, bytes]:
        return await self.request(endpoint, data=data or {}, method='PUT', **kwargs)

    async def delete(self, endpoint: str, **kwargs: Any) -> Union[Dict[str, Any], str, int, bytes]:
        return await self.request(endpoint, method='DELETE', **kwargs)
