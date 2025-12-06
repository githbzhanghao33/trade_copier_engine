# Trade Copier Engine

A self-hosted **multi-exchange futures copy-trading engine** that mirrors trades
from one "master" futures account to multiple follower accounts.

It has been tested in real environments and can copy trades with near real-time
latency (seconds level), with basic safety checks and group-based configuration.

> ⚠️ **High risk disclaimer**
>
> Futures trading is highly risky and can cause severe losses.
> This project is a personal side project for self-hosted use.
> It is **not** a financial product and **not** investment advice.

---

## Features

- ✅ **Multi-exchange support**
  - Separate adapters for exchange-style APIs (e.g. Binance / OKX / Gate-style)
- ✅ **Group-based copy trading**
  - One master account per group
  - Multiple follower accounts per group with individual ratios and settings
- ✅ **Basic risk & safety controls**
  - Notional limits per order and per account (max_single / max_total)
  - Quantity / price precision handling per exchange
  - Slippage checks to avoid chasing bad prices
  - Circuit breaker with retry & dead-letter logging
- ✅ **Authorization & configuration**
  - Auth codes mapped to groups
  - Configs stored in a database as JSON (per group)
  - YAML example configs for quick bootstrap
- ✅ **Observability**
  - Group-level log files
  - Heartbeat files
  - Simple metrics endpoint
  - SSE endpoint for live positions

This repository contains the **core engine and backend services** that I use
for my own experiments and self-hosted setups.

It is good enough for small teams or power users who understand both
the trading and the technical risks.

---

## Architecture

High-level architecture:

```text
               +------------------------------+
               |        HTTP / Flask API      |
               |  - auth (X-Auth-Code)        |
               |  - config (per group)        |
               |  - metrics                   |
               |  - manual close, SSE stream  |
               +------------------------------+
                           |
                           v
      +-----------------------------------------------+
      |          Engine manager (per group)           |
      |  - load config from DB                        |
      |  - create TradeCopierEngine for each group    |
      +----------------------+------------------------+
                             |
                  (one thread per group)
                             v
      +-----------------------------------------------+
      |         TradeCopierEngine (core loop)         |
      |  - poll master positions via REST             |
      |  - compute deltas                             |
      |  - apply risk checks                          |
      |  - send orders to follower accounts           |
      +----------------------+------------------------+
                             |
                             v
      +-----------------------------------------------+
      |          Exchange REST / WebSocket APIs       |
      |  - Binance-style / OKX-style / Gate-style     |
      +-----------------------------------------------+


      +-----------------------------------------------+
      |                WSBroker                       |
      |  - manage WS connections per account/suffix   |
      |  - cache positions / tickers                  |
      |  - provide data to SSE & metrics              |
      +-----------------------------------------------+

      +-----------------------------------------------+
      |              Database (SQLite)                |
      |  - configs table: group, auth_code, data JSON |
      +-----------------------------------------------+
