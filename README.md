<div align="center">


![Logo](https://github.com/wynoislive/wavelink-wyno/blob/main/docs/_static/logo.png)

![Python Version](https://img.shields.io/pypi/pyversions/wavelink-wyno)
[![PyPI - Version](https://img.shields.io/pypi/v/wavelink-wyno)](https://pypi.org/project/wavelink-wyno/)
[![Github License](https://img.shields.io/github/license/wynoislive/wavelink-wyno)](LICENSE)
[![Lavalink Version](https://img.shields.io/badge/Lavalink-v4.0%2B-blue?color=%23FB7713)](https://lavalink.dev)
![Lavalink Plugins](https://img.shields.io/badge/Lavalink_Plugins-Native_Support-blue?color=%2373D673)


</div>


Wavelink-Wyno is a robust and powerful Lavalink wrapper for [Discord.py](https://github.com/Rapptz/discord.py)
Wavelink-Wyno features a fully asynchronous API that's intuitive and easy to use, with full support for Discord's DAVE (E2EE) Voice Protocol.


#  Version 4.0.0:

[Migrating Guide](https://wavelink.dev/en/latest/migrating.html)


### Features

- Full asynchronous design.
- Lavalink v4+ Supported with REST API.
- discord.py v2.0.0+ Support.
- Advanced AutoPlay and track recommendations for continuous play.
- Object orientated design with stateful objects and payloads.
- Fully annotated and complies with Pyright strict typing.
- **DAVE Protocol (E2EE) compatible** — requires Lavalink v4.0.8+.


## Getting Started

**See Examples:** [Examples](https://github.com/wynoislive/wavelink-wyno/tree/main/examples)

**Lavalink:** [GitHub](https://github.com/lavalink-devs/Lavalink/releases), [Webpage](https://lavalink.dev)


## Documentation

[Official Documentation](https://wavelink.dev/en/latest)

## Support

For support using Wavelink-Wyno, please join the official [Support Server](https://discord.gg/9WJSP4Kqg4) on
[Discord](https://discordapp.com)

[![Discord Banner](https://discordapp.com/api/guilds/490948346773635102/widget.png?style=banner2)](https://discord.gg/9WJSP4Kqg4)


## Installation

**Wavelink-Wyno requires Python 3.10+**

**Windows**


```sh
py -3.10 -m pip install -U wavelink-wyno
```

**Linux**

```sh
python3.10 -m pip install -U wavelink-wyno
```

**Virtual Environments**

```sh
pip install -U wavelink-wyno
```


## Lavalink

Wavelink-Wyno requires **Lavalink v4.0.8+** for Discord's DAVE (E2EE) Voice Protocol support.
See: [Lavalink](https://github.com/lavalink-devs/Lavalink/releases)

For spotify support, simply install and use [LavaSrc](https://github.com/topi314/LavaSrc) with your `wavelink.Playable`


### Notes

- Wavelink-Wyno is compatible with Lavalink **v4.0.8+**.
- Wavelink-Wyno has built in support for Lavalink Plugins including LavaSrc and SponsorBlock.
- Wavelink-Wyno is fully typed in compliance with Pyright Strict, though some nuances remain between discord.py and wavelink.
