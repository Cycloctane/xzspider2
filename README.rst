xzspider2
=========

新的阿里云先知社区爬虫。附带docker。

Usage
-----

用docker。

.. code-block:: bash

    docker build -t xzspider2 .
    mkdir ./xzdocs
    docker run --user=$(id --user):$(id --group) -v ./xzdocs:/opt/xzdocs/:rw --rm xzspider2 --pages 1,2,4-6
