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





Getting Started
1. Clone and install dependencies
git clone https://github.com/<yourname>/trade_copier_engine.git
cd trade_copier_engine

python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate

pip install -r requirements.txt

2. Configure environment

Copy the example env file:

cp .env.example .env


By default it uses a local SQLite database:

DATABASE_URL=sqlite:///./configs.db

3. Prepare configs

Use the example configs under configs/:

cp configs/groupA.example.yml configs/groupA.yml
cp configs/groupB.example.yml configs/groupB.yml


Edit them with your own API keys, ratios and risk settings.

4. Initialize auth codes (optional)

If you want to use auth codes, you can generate some and store them in the DB
(using the provided script or your own tooling).
Each auth code is mapped to a group in the configs table.

5. Run the server
python server.py


This will:

load all configs from the database,

start a TradeCopierEngine for each configured group,

start the WSBroker,

start the Flask HTTP API.

Status

Right now this is a self-hosted core engine that is good enough for
small teams / personal use.

It is NOT a full enterprise-grade trading system (yet).

Possible future work:

PnL-based risk controls (daily loss limits, drawdown limits)

Better monitoring & alerting (Prometheus / Grafana, Telegram alerts)

Optional PostgreSQL / Redis storage backends

High-availability / multi-instance deployment

More tests and CI

If you are interested in improving it, ideas and PRs are welcome.

License

Choose a license you are comfortable with (MIT / Apache-2.0 / etc.) and
mention it here.

