# Hosted sample build for the hackathon judges.
# The image holds only the files allowed by .dockerignore. It never runs the bot,
# never reads your keys, and serves invented sample data (see judge/seed.py).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Safety net: stop the build if anything personal got in.
RUN for f in .env state.db persona.md certs assets/reactions assets/drafts; do \
      if [ -e "$f" ]; then echo "forbidden file in judge image: $f"; exit 1; fi; done \
 && if ls assets/profile_* >/dev/null 2>&1; then echo "forbidden: personal photo in judge image"; exit 1; fi

# Build the invented sample database and the folders the app writes into.
RUN ABRA_JUDGE_MODE=1 python judge/seed.py && mkdir -p generated storyboards

ENV ABRA_JUDGE_MODE=1 ABRA_HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "liveview.py"]
