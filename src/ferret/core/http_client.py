from enum import Enum
from typing import Any

from niquests.async_api import AsyncSession


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"

    CONNECT = "CONNECT"
    TRACE = "TRACE"

    def __str__(self) -> str:
        return self.value


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ):
        self.base_url = base_url
        self.params = params
        self.headers = headers
        self.cookies = cookies

        client_kwargs: dict[str, Any] = {}
        if base_url:
            client_kwargs["base_url"] = base_url
        if self.headers:
            client_kwargs["headers"] = self.headers
        if self.cookies:
            client_kwargs["cookies"] = self.cookies

        self.client = AsyncSession(**client_kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.close()

    async def send(
        self,
        method: str,
        url: str,
        *,
        data: Any | None = None,
        files: Any | None = None,
        json: dict | None = None,
        params: Any | None = None,
        headers: Any | None = None,
        cookies: Any | None = None,
    ):
        response = await self.client.request(
            method=method,
            url=url,
            data=data,
            files=files,
            json=json,
            params=params,
            headers=headers,
            cookies=cookies,
        )
        return response


if __name__ == "__main__":

    def main() -> None:
        """测试 HttpClient：等价 curl -X GET https://httpbin.org/get -H "accept: application/json" """
        import asyncio

        async def run() -> None:
            async with HttpClient() as client:
                resp = await client.send(
                    HttpMethod.GET.value,
                    "https://httpbun.com/get",
                    headers={"accept": "application/json"},
                )
                print("status:", resp.status_code)
                print("http_version:", resp.http_version)
                print("body:", resp.text)

        asyncio.run(run())

    main()
