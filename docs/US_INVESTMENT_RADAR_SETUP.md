# US Investment Radar Setup

This deployment turns `daily_stock_analysis` into a US-market radar for:

- existing WealthBrain holdings: `AVGO,BABA,CRWV,OKLO,PLTR,QQQ,SNDK,TSM,VRT`
- core US watchlist names across AI, mega-cap tech, finance, healthcare, and consumer
- market sentiment proxies such as `VIX`, `TLT`, `HYG`, `UUP`, `GLD`, and sector ETFs

It is not an auto-trading system. Reports should be treated as research input and risk alerts only.

## GitHub Actions Configuration

Use these repository Variables:

```text
OPENAI_MODEL=gpt-5.4-mini
REPORT_LANGUAGE=zh
MARKET_REVIEW_REGION=us
MARKET_REVIEW_ENABLED=true
REPORT_TYPE=full
REPORT_PROFILE=us_investment_radar
REPORT_SUMMARY_ONLY=false
SINGLE_STOCK_NOTIFY=false
PORTFOLIO_STOCK_LIST=AVGO,BABA,CRWV,OKLO,PLTR,QQQ,SNDK,TSM,VRT
OPPORTUNITY_MAX=8
RISK_WATCH_MAX=8
MAX_WORKERS=1
ANALYSIS_DELAY=5
STOCK_LIST=NVDA,MSFT,AAPL,AMZN,GOOGL,META,TSLA,AMD,AVGO,TSM,PLTR,JPM,V,LLY,COST,SPY,QQQ,SMH,TLT,GLD,BABA,CRWV,OKLO,SNDK,VRT,SPX,NASDAQ,VIX,IWM,XLK,XLF,XLE,HYG,UUP
```

Use these repository Secrets:

```text
OPENAI_API_KEY=your OpenAI API key
TELEGRAM_BOT_TOKEN=your Telegram bot token
TELEGRAM_CHAT_ID=your Telegram chat id
```

Recommended optional Secrets:

```text
TAVILY_API_KEYS=your Tavily key
BRAVE_API_KEYS=your Brave Search key
```

`TAVILY_API_KEYS` is recommended for this profile. Without it, the report
still ranks holdings and opportunities, but news catalysts may degrade to
technical signals and show "data missing" more often.

## Assisted Setup

After forking this repository and authenticating GitHub CLI:

```bash
gh auth login
scripts/configure_us_investment_radar_gh.sh your-github-name/daily_stock_analysis
```

The script stores non-sensitive values as Variables and prompts for sensitive values as Secrets.

## First Test Run

Run the workflow manually:

```bash
gh workflow run daily_analysis.yml --repo your-github-name/daily_stock_analysis -f mode=full -f force_run=true
```

Then watch recent runs:

```bash
gh run list --repo your-github-name/daily_stock_analysis --workflow daily_analysis.yml --limit 3
```

The Telegram report should include:

- individual analysis for the WealthBrain holdings
- an opportunity radar for non-holding candidates
- a risk radar for VIX, rates, dollar, gold, credit, and sector proxies
- US market review with VIX and broad index context
- semiconductor/AI infrastructure read-through from `SMH`, `AVGO`, `TSM`, `SNDK`, `VRT`, `CRWV`, and `OKLO`
- rate, dollar, gold, and credit-risk context from `TLT`, `UUP`, `GLD`, and `HYG`
- conditional suggestions and explicit risk warnings, not unconditional buy/sell instructions
