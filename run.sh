
mkdir -p logs
nohup python server.py >> logs/out.log 2>&1 &
