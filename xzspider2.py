#! /usr/bin/env python3

import asyncio
import binascii
import logging
import os
import re
from collections.abc import Awaitable
from functools import partial
from hashlib import sha256
from os.path import splitext
from pathlib import Path
from urllib.parse import unquote

import anyio
import magic
import orjson
from aiohttp import ClientResponseError, ClientSession, ClientTimeout
from bs4 import BeautifulSoup, Tag
from bs4.element import AttributeValueList
from markdownify import MarkdownConverter
from yarl import URL

__version__ = "0.2.0"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"
BASE_URL = URL("https://xz.aliyun.com/api/v2/news/")

IMAGE_DATA_RE = re.compile(r"^data:(image/[a-z]{3,4});base64,")
ARG1_RE = re.compile(rb"^<textarea id=\"renderData\" style=\"display:none\">{\"l1\":\"var arg1='([\da-f]{50})';\"}")
IMAGE_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/bmp": ".bmp",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("XZSpider")


def _parse_url(urlstr: str) -> URL | None:
    try:
        url = URL(urlstr)
    except ValueError:  # invalid URL
        return
    if url.absolute and url.scheme in ("http", "https"):
        return url


class XZSpider:

    def __init__(
        self,
        *,
        save_path: str,
        index_file: str | None = None,
        ignore_exists: bool = False,
        limit: int = 3,
        page_limit: int = 2,
        timeout: int = 20,
    ) -> None:
        self.save_path = Path(save_path)
        self.ignore_exists = ignore_exists
        self._md = MarkdownConverter()
        self._client = ClientSession(
            base_url=BASE_URL, headers={"User-Agent": UA}, timeout=ClientTimeout(connect=timeout)
        )
        self._page_sem = asyncio.Semaphore(page_limit)
        self._news_sem = asyncio.Semaphore(limit)
        self._image_sem = asyncio.Semaphore(limit * 8)
        self._cookie_proc_lock = asyncio.Lock()
        self._cookie_proc: asyncio.subprocess.Process | None = None
        self.fetched_index: dict[int, str] = {}
        if not ignore_exists and (index_file or (self.save_path / "index.json").exists()):
            with open(index_file or self.save_path / "index.json", "rb") as f:
                self.fetched_index = {int(k): v for k, v in orjson.loads(f.read()).items()}
            logger.info(f"Loaded existing index with {len(self.fetched_index)} articles")

    def save_index(self, index_path: str | None = None) -> None:
        with open(index_path or self.save_path / "index.json", "wb") as f:
            f.write(
                orjson.dumps(
                    {
                        str(k): self.fetched_index[k]
                        for k in sorted(self.fetched_index, reverse=True)
                    },
                    option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE
                )
            )

    async def _terminate_cookie_proc(self) -> None:
        if self._cookie_proc is not None:
            logger.warning(f"Terminating cookie generator (pid {self._cookie_proc.pid})...")
            if self._cookie_proc.stdin is not None:
                self._cookie_proc.stdin.close()
            self._cookie_proc.terminate()
            try:
                await asyncio.wait_for(self._cookie_proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self._cookie_proc.kill()
                await self._cookie_proc.wait()
            finally:
                self._cookie_proc = None

    async def close(self) -> None:
        await asyncio.gather(self._client.close(), self._terminate_cookie_proc())

    async def _update_cookie(self, arg1: str) -> None:
        async with self._cookie_proc_lock:
            if self._cookie_proc is None:
                self._cookie_proc = await asyncio.create_subprocess_exec(
                    "node", str(Path(__file__).with_name("adapter.js")),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
                logger.info(f"Cookie generator started. pid {self._cookie_proc.pid}")

            self._cookie_proc.stdin.write((arg1 + "\n").encode())
            await self._cookie_proc.stdin.drain()
            try:
                line = await asyncio.wait_for(self._cookie_proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                logger.error("Cookie generator timeout")
                await self._terminate_cookie_proc()
                return

        cookie = line.decode().strip()
        if not cookie:
            logger.error("Empty cookie from cookie generator")
            return
        self._client.cookie_jar.update_cookies({"acw_sc__v2": cookie})
        logger.info(f"Updated acw_sc__v2 cookie.")

    async def init_cookie(self) -> None:
        res = await self._make_article_req(18812, retry=True)
        if res is None:
            raise ValueError("Failed to initialize cookies")

    async def _get_remote_image(
        self, url: URL, title: str, soup: BeautifulSoup, element: Tag
    ) -> None:
        if url.host == "xianzhi.aliyuncs.com":
            url = url.with_host("xzfile.aliyuncs.com")
        elif re.match(r"w[wsx](\d+)\.sinaimg\.cn", url.host):
            url = url.with_host("tva1.sinaimg.cn")
        referer = {
            "sinaimg.cn": "https://weibo.com/", "jianshu.io": "https://www.jianshu.com/",
            "csdnimg.cn": "https://blog.csdn.net/", "cnblogs.com": "https://www.cnblogs.com/",
            "gitee.com": "https://gitee.com/", "52pojie.cn": "https://www.52pojie.cn/",
        }.get(".".join(url.host.rsplit(".", 2)[-2:]), "https://xz.aliyun.com/")

        async with self._image_sem:
            async with self._client.get(
                url, headers={"Accept": "image/*", "Referer": referer}
            ) as resp:
                if not resp.ok:
                    element.decompose()
                    resp.raise_for_status()
                    return

                data = await resp.read()
                ext = splitext(url.path.removesuffix("!post").removesuffix("!thumbnail"))[1].lower()
                if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".svg"}:
                    ext = IMAGE_MIME.get(resp.content_type) \
                        or IMAGE_MIME.get(magic.from_buffer(data, mime=True))
            if not ext:
                element.decompose()
                raise ValueError("Cannot determine image format of " + str(url))

            src = "img/" + sha256(str(url).encode()).hexdigest() + ext
            if not os.path.exists(self.save_path / title / src):
                element.replace_with(soup.new_tag("img", src=src, alt=str(url)))
                async with await anyio.open_file(self.save_path / title / src, "wb") as f:
                    await f.write(data)

    async def _save_embedded_image(
        self, data: str, title: str, soup: BeautifulSoup, element: Tag
    ) -> None:
        if not (m := IMAGE_DATA_RE.match(data)) or not (ext := IMAGE_MIME.get(m.group(1))):
            element.decompose()
            raise ValueError("Cannot determine image format for embedded image")

        async with self._image_sem:
            try:
                raw_data = binascii.a2b_base64(data[m.end():], strict_mode=True)
            except binascii.Error:
                element.decompose()
                raise ValueError("Invalid base64 data for embedded image")
            src = "img/" + sha256(raw_data).hexdigest() + ext
            if not os.path.exists(self.save_path / title / src):
                element.replace_with(soup.new_tag("img", src=src, alt="embedded image"))
                async with await anyio.open_file(self.save_path / title / src, "wb") as f:
                    await f.write(raw_data)

    async def _make_article_req(
        self, idx: int, *, retry: bool = False
    ) -> dict[str, str] | None:
        async with self._client.get(str(idx)) as resp:
            if resp.status != 200 or "Set-Cookie" not in resp.headers:
                logger.warning(f"Rate limit exceeded or blocked when fetching article {idx}")
                return
            body = await resp.read()
            if m := ARG1_RE.match(body):
                if not retry:
                    logger.error(f"Failed to fetch article {idx} - invalid cookies")
                    return
                await self._update_cookie(m.group(1).decode())
                return await self._make_article_req(idx, retry=False)
        try:
            json_obj = orjson.loads(body)
            if json_obj["content"] and json_obj["title"]:
                return json_obj
        except (orjson.JSONDecodeError, TypeError, KeyError):
            logger.error(f"Failed to fetch article {idx} - invalid response")

    async def _fetch_article(self, idx: int) -> bool:
        json_obj = await self._make_article_req(idx, retry=True)
        if not json_obj:
            return False

        title = str(idx) + "." + \
            json_obj['title'].translate(str.maketrans({c: "_" for c in "\\/:*?\"'<>| "}))
        (self.save_path / title / "img").mkdir(parents=True, exist_ok=True)

        soup = BeautifulSoup(json_obj["content"], "lxml")
        tasks: list[Awaitable[None]] = []

        remote_img = partial(self._get_remote_image, title=title, soup=soup)
        embedded_img = partial(self._save_embedded_image, title=title, soup=soup)

        for element in soup.select("img[src]"):
            value = element["src"]
            src: str = value[0] if isinstance(value, AttributeValueList) else value
            if src.startswith("data:"):
                tasks.append(embedded_img(data=src, element=element))
            elif (url := _parse_url(src)) is not None:
                tasks.append(remote_img(url=url, element=element))
            else:
                element.decompose()

        for element in soup.select('card[name="image"][value]'):
            value = element["value"]
            if isinstance(value, AttributeValueList):
                value = value[0]
            try:
                src = orjson.loads(unquote(value.removeprefix("data:")))["src"]
            except (orjson.JSONDecodeError, TypeError, KeyError):
                element.decompose()
                continue
            if src.startswith("data:"):
                tasks.append(embedded_img(data=src, element=element))
            elif (url := _parse_url(src)) is not None:
                tasks.append(remote_img(url=url, element=element))
            else:
                element.decompose()

        for element in soup.select('card[name="codeblock"][value]'):
            value = element["value"]
            if isinstance(value, AttributeValueList):
                value = value[0]
            try:
                code: str = orjson.loads(unquote(value.removeprefix("data:")))["code"]
            except (orjson.JSONDecodeError, TypeError, KeyError):
                element.decompose()
            else:
                element.replace_with(soup.new_tag("pre", string=code))

        errors = await asyncio.gather(*tasks, return_exceptions=True)
        for e in errors:
            if e:
                logger.warning(f"Cannot download image in {title}: {str(e)}")

        async with await anyio.open_file(
            self.save_path / title / (title + ".md"), "w", encoding="utf-8"
        ) as f:
            await f.write(self._md.convert_soup(soup))
        self.fetched_index[idx] = json_obj["title"]
        return True

    async def fetch_article(self, idx: int) -> bool:
        async with self._news_sem:
            return await self._fetch_article(idx)

    async def fetch_page_links(self, page: int) -> set[int] | None:
        async with self._client.get("", params={"page": page}) as resp:
            try:
                resp.raise_for_status()
                json_obj = await resp.json(loads=orjson.loads)
                return set(int(item["id"]) for item in json_obj)
            except (ClientResponseError, orjson.JSONDecodeError, TypeError, KeyError):
                logger.error(f"Failed to fetch page {page} - invalid response")

    async def fetch_page(self, page: int) -> None:
        async with self._page_sem:
            links = await self.fetch_page_links(page)
            if not links:
                logger.warning(f"No articles on page {page}")
                return
            res = await asyncio.gather(
                *(
                    self.fetch_article(i) for i in links
                    if self.ignore_exists or i not in self.fetched_index
                )
            )
        logger.info(
            f"Page {page}: {len(links)} total, {sum(1 for i in res if i)} downloaded, "
            f"{sum(1 for i in res if not i)} failed, {len(links) - len(res)} skipped"
        )


def _parse_pages(value: str) -> set[int]:
    pages: set[int] = set()
    for part in value.split(","):
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    return pages


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="xz.aliyun.com articles scraper")
    parser.add_argument(
        "--pages", required=True, help="Pages to scrape, e.g. '1,2,4-6'"
    )
    parser.add_argument(
        "--output", "-o", required=True, help="Directory to save downloaded articles"
    )
    parser.add_argument(
        "--index-file", default=None,
        help="Path to existing index.json file (to skip existing articles in index)",
    )
    parser.add_argument(
        "--ignore-exists", action="store_true",
        help="Ignore existing items in index.json and re-download all articles"
    )
    parser.add_argument(
        "--limit", type=int, default=3, help="Maximum concurrent scraping articles (default 3)"
    )
    parser.add_argument(
        "--page-limit", type=int, default=2, help="Maximum concurrent scraping pages (default 2)"
    )
    parser.add_argument(
        "--timeout", type=int, default=20, help="Connect timeout in seconds (default 20)"
    )
    ns = parser.parse_args()
    pages = _parse_pages(ns.pages)
    crawler = XZSpider(
        save_path=ns.output,
        index_file=ns.index_file,
        limit=ns.limit,
        page_limit=ns.page_limit,
        ignore_exists=ns.ignore_exists,
        timeout=ns.timeout,
    )
    try:
        await crawler.init_cookie()
        await asyncio.gather(*(crawler.fetch_page(i) for i in pages))
    finally:
        await crawler.close()
        crawler.save_index()


if __name__ == "__main__":
    asyncio.run(main())
