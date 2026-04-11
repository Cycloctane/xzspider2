#! /usr/bin/env python3

import asyncio
from typing import override

import undetected_chromedriver as uc

from xzspider2 import XZSpider, logger, main


class XZSpiderChrome(XZSpider):

    def _update_cookie_chromium(self, article_id: int) -> None:
        driver = uc.Chrome()
        try:
            driver.get(f"https://xz.aliyun.com/api/v2/news/{article_id}")
            logger.warning("Please solve challenge in browser. Then press Enter to continue...")
            input()
            self._client.cookie_jar.update_cookies({cookie["name"]: cookie["value"] for cookie in driver.get_cookies()})
            logger.info(f"Updated cookies from browser")
        finally:
            driver.quit()

    @override
    async def init_cookie(self) -> None:
        self._update_cookie_chromium(18812)
        await super().init_cookie()

if __name__ == "__main__":
    asyncio.run(main(XZSpiderChrome))
