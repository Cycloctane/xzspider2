xzspider2
=========

新的阿里云先知社区爬虫。

Usage
-----

.. code-block:: bash

    python3 xzspider2.py -o ./xzdocs/ --pages 1-6

如果下载到一半提示"Rate limit exceeded or blocked ..."或者"Failed to initialize cookies"。可以尝试换ip。

或者用xzspider2_chrome.py

.. code-block:: bash

    pip install undetected-chromedriver
    python3 xzspider2_chrome.py -o ./xzdocs/ --pages 1-6

先在弹出的浏览器里划一下验证码。然后回到命令行按Enter。
