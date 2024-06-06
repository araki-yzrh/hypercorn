
PORT=18888
M=1
W=1
C=1
N=1

run:
	python runner app:app --bind 0.0.0.0:${PORT} --access-logfile - --error-logfile - --max-requests ${M} --workers ${W}

test:
	ab -c ${C} -n ${N} http://localhost:${PORT}/

testauth:
	ab -c 8 -n 8 http://localhost:8081/oauth2/token

testauthtest:
	ab -c 8 -n 8 http://localhost:8081/test
