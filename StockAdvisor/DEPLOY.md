# StockAdvisor -- Deployment Checklist

A local, paper-only, human-approved trading assistant. This checklist gets it
running on your Windows PC with no command line needed after the initial copy.

## Before you start

- [ ] You have this whole `StockAdvisor` folder copied somewhere on your PC
      (e.g. `C:\Users\you\StockAdvisor`).
- [ ] You have an internet connection (needed for free Yahoo Finance price data).
- [ ] You do NOT need any paid API key, broker account, or credit card for
      the default setup.

## One-time setup

- [ ] Double-click **setup.bat**.
- [ ] If it says Python is missing: install Python 3.10+ from
      https://www.python.org/downloads/ and make sure you check
      **"Add python.exe to PATH"** during install. Re-run setup.bat.
- [ ] Wait for it to finish creating `.venv` and installing packages
      (first run can take a few minutes).
- [ ] Confirm it created a `.env` file in the folder (it copies `.env.example`
      automatically). You do not need to edit this file to get started --
      the defaults are safe: paper mode, no API keys, free reflection.

## Every time you want to run it

- [ ] Double-click **start.bat**.
- [ ] Two console windows open: "StockAdvisor Worker" and
      "StockAdvisor Dashboard". Leave them running in the background.
- [ ] Your browser opens automatically to http://127.0.0.1:8000 -- this is
      the dashboard. Nothing here talks to the cloud; it's your own PC
      talking to itself on localhost.
- [ ] To stop everything, just close both console windows (or press
      Ctrl+C in each).

## What to expect the first week

- [ ] The book starts fully in cash ($100,000 paper money by default --
      change `PAPER_STARTING_CASH` in `.env` before first run if you want
      a different amount).
- [ ] The worker ranks the 11 sector ETFs daily but only PROPOSES new buys
      on the weekly rebalance day (Friday). Don't be surprised if the
      approval queue is empty most days -- that is normal and by design.
- [ ] When a buy is proposed, you'll see it in the "Approval Queue" on the
      dashboard, and your browser will show an alert. Click Approve or
      Reject -- nothing is bought without your click.
- [ ] A "Morning Brief" appears on the dashboard each weekday.

## Confirming paper-only safety for yourself

- [ ] Open `.env` in Notepad. Confirm `ALLOW_LIVE_TRADING=false` and
      `LIVE_TRADING_CONFIRMED_I_UNDERSTAND_THE_RISK=false`.
- [ ] There is no code anywhere in this repo that can place a real
      brokerage order -- search for "broker" or "execution adapter" and
      you will find none. These flags are a documented safety gate for a
      feature that does not exist yet, not a switch you can flip today.

## Optional: turning on the LLM reflection "brain"

Leave this off unless you specifically want it -- it costs API money and
is not required for the system to work.

- [ ] Set `REFLECTION_MODE=llm` in `.env`.
- [ ] Set `ANTHROPIC_API_KEY=...` in `.env`.
- [ ] Install and configure the `hermes` CLI so it's available on your PATH.
- [ ] If `hermes` is missing or errors, the system automatically falls back
      to the free rule-based reflection for that cycle -- it will not block.

## Backing up / moving your data

- [ ] Everything (paper book, trade history, approval queue, reflection
      history, briefs) lives in `data\stockadvisor.db`. Copy that one file
      to back up or move your history.
- [ ] `data\cache\*.csv` is just a price cache and can be deleted safely --
      it will be re-downloaded from Yahoo Finance automatically.

## If something looks wrong

- [ ] Check the "StockAdvisor Worker" console window for the most recent
      log lines -- it prints what it's doing every ~20 seconds.
- [ ] Check the "Recent events log" panel on the dashboard (under
      Specs / Behavior Reference).
- [ ] If the dashboard shows a black-swan halt banner, that is the system
      protecting your capital as designed -- it will auto-resume when SPY
      recovers above its 200-day average, or you can click Resume once
      you've reviewed the situation yourself.
