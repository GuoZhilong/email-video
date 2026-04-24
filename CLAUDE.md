# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Minimal Python utility for making authenticated requests to the NetEase Leihuo AI platform (`ai.leihuo.netease.com`).

## Running

```bash
pip install requests
python request.py
```

## Architecture

Two files:

- `request.py` — reads `cookies.txt`, POSTs to `/webapi/ai_account/token`, prints response
- `cookies.txt` — contains the raw `QAWEB_SESS` cookie value (not committed; update when session expires)

Authentication is cookie-based: the `QAWEB_SESS` value from `cookies.txt` is sent as the `Cookie` header.
