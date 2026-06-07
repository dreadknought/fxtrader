# FXTrader Fargate Ops Scripts

Copy the env example once:

```bash
cp deploy/fxtrader.env.example deploy/fxtrader.env
nano deploy/fxtrader.env
source deploy/fxtrader.env
```

Common commands:

```bash
./deploy/status.sh                  # show mode, schedule, latest task def, recent tasks
./deploy/run-once.sh                # start one task now
./deploy/tail-logs.sh               # follow recent logs
./deploy/logs-today.sh              # print logs since midnight New York time
./deploy/set-mode.sh practice       # safe mode, no live trading
./deploy/set-mode.sh live           # requires typing ENABLE LIVE
./deploy/deploy-image.sh            # build/push image, register task def, update schedule
./deploy/create-or-update-schedule.sh
```

Notes:

- `OANDA_ENV` and `ENABLE_LIVE_TRADING` are plain container environment variables.
- OANDA tokens/account IDs stay in AWS Secrets Manager.
- Switching modes registers a new ECS task definition revision and updates the EventBridge schedule to use it.
- The scheduled live switch intentionally requires manual confirmation.
