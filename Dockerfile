FROM node:22-slim

WORKDIR /app

RUN apt-get update && apt-get install -y git ca-certificates && rm -rf /var/lib/apt/lists/*

COPY package*.json .npmrc ./
RUN npm install --omit=dev

COPY . .

CMD ["node", "personal_line_bot.js"]
