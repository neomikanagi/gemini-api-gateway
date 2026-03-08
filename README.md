# Gemini API Gateway

```bash
mkdir -p /docker/gemini-api-gateway/config
cd /docker/gemini-api-gateway

cat > config/accounts.json <<EOF
[
  {
    "__Secure-1PSID": "FIRST_ACCOUNT_PSID",
    "__Secure-1PSIDTS": "FIRST_ACCOUNT_PSIDTS"
  },
  {
    "__Secure-1PSID": "SECOND_ACCOUNT_PSID",
    "__Secure-1PSIDTS": "SECOND_ACCOUNT_PSIDTS"
  }
]
EOF

docker run -d \
  --name gemini-gateway \
  --restart always \
  -p 8000:8000 \
  --log-driver json-file \
  --log-opt max-size=2m \
  --log-opt max-file=1 \
  -v /docker/gemini-api-gateway/config:/app/config \
  ghcr.io/YOUR_USERNAME/gemini-api-gateway:latest
