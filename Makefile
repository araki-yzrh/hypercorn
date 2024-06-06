
PORT=18888
R=1
W=1
N=1

run:
	python runner app:app --bind 0.0.0.0:${PORT} --access-logfile - --error-logfile - --max-requests ${R} --workers ${W}

test:
	ab -c ${N} -n ${N} http://localhost:${PORT}/
