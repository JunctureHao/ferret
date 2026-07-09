from enum import Enum

import httpx2
from httpx2._types import (
    CookieTypes,
    HeaderTypes,
    QueryParamTypes,
    RequestContent,
    RequestData,
    RequestFiles,
)


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
        http2: bool = True,
    ):
        self.base_url = base_url
        self.params = params
        self.headers = headers
        self.cookies = cookies

        self.client = httpx2.AsyncClient(
            base_url=base_url, http2=http2, headers=self.headers, cookies=self.cookies
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def send(
        self,
        method: str,
        url: str,
        *,
        content: RequestContent | None = None,
        data: RequestData | None = None,
        files: RequestFiles | None = None,
        json: dict | None = None,
        params: QueryParamTypes | None = None,
        headers: HeaderTypes | None = None,
        cookies: CookieTypes | None = None,
    ):
        response = await self.client.request(
            method=method,
            url=url,
            content=content,
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
