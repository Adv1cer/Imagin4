# Deploying the backend to a small test VPS

Goal: get the backend reachable at a real IP so your หัวหน้า can hit it from
Postman/browser instead of `localhost`. This is NOT a production deployment guide
(no HA, no backups, no TLS by default) -- just enough to run a real small-scale test.

I can't rent/pay for the server myself (no payment access) -- this is the setup to run
once you've provisioned one yourself.

## 0. Do you even need a VPS? (if หัวหน้า is on the same มหาลัย network)

If หัวหน้าอยู่ในเครือข่ายเดียวกับนายท่าน (same campus WiFi/LAN) ตอนที่จะทดสอบ ไม่ต้องเช่าเครื่องเลยก็ได้ค่ะ -- แค่เปิด backend ให้ฟังบน `0.0.0.0` แทน `127.0.0.1` แล้วให้หัวหน้ายิงไปที่ IP วงในของเครื่องนายท่านแทน `localhost`:

1. หา LAN IP ของเครื่อง: `ip addr` (Linux) หรือ `ipconfig` (Windows) -- เช่น `10.x.x.x` หรือ `192.168.x.x`
2. ตรวจว่า `docker-compose.yml` แม็พพอร์ต `api` เป็น `8000:8000` อยู่แล้ว (ปกติ default มันฟังทุก interface อยู่แล้วถ้าไม่ได้ bind เป็น `127.0.0.1:8000:8000` โดยเฉพาะ) -- เช็คในไฟล์ก่อนได้เลยค่ะ
3. ให้หัวหน้าใช้ `http://<lan-ip>:8000` เป็น base URL แทน
4. เพิ่ม origin ของหัวหน้า (ถ้าทดสอบผ่านเบราว์เซอร์) ใน `APP_CORS_ALLOW_ORIGINS_CSV`

**ข้อควรระวัง**: เน็ตมหาลัยหลายที่เปิด **client isolation** บน WiFi (กันเครื่องในวงเดียวกันคุยกันเอง เพื่อความปลอดภัย) โดยเฉพาะ WiFi หอ/สาธารณะ -- ถ้าเป็นแบบนี้หัวหน้าจะ ping/connect เข้าเครื่องนายท่านไม่ได้เลยแม้อยู่ WiFi เดียวกัน ลองทดสอบง่ายๆ ก่อนคือให้หัวหน้า ping LAN IP ดูก่อนว่าถึงไหม

ถ้าติด client isolation หรือหัวหน้าไม่ได้อยู่ในสถานที่เดียวกันแล้ว มีตัวเลือกที่ยังไม่ต้องเช่า VPS อีกทาง คือ tunnel ชั่วคราว (ฟรี ไม่ต้องผูกบัตร):
- `cloudflared tunnel --url http://localhost:8000` (Cloudflare Tunnel) หรือ `ngrok http 8000` -- ได้ URL สาธารณะชั่วคราวชี้เข้าเครื่อง local ทันที เหมาะกับการให้หัวหน้าลองไม่กี่ชั่วโมง แต่เครื่องนายท่านต้องเปิดค้างและต่อเน็ตตลอดเวลาที่ทดสอบ

ถ้าอยากได้อะไรที่เสถียรกว่านั้น (เครื่องนายท่านปิดได้ ไม่ขึ้นกับว่าหัวหน้าอยู่เน็ตไหน) ค่อยไปเช่า VPS ตามขั้นตอนด้านล่างค่ะ

## 1. Pick a VPS

No GPU needed for this test -- ComfyUI stays on your DGX Spark; the VPS only runs the
API/DB/queue/storage stack (`backend/docker-compose.yml`).

| Provider | Region close to Thailand | Rough cost for 2 vCPU / 4GB / 80GB |
| --- | --- | --- |
| Vultr | Singapore | ~$20-24/mo (or hourly, cheaper for a short test) |
| DigitalOcean | Singapore | ~$24/mo |
| Hetzner Cloud | EU only (higher latency to TH) | ~€10/mo (cheapest, but latency matters if the boss is testing responsiveness) |
| Linode/Akamai | Singapore | ~$24/mo |

Recommendation for a short test: **Vultr or DigitalOcean, Singapore region, Ubuntu
22.04 LTS, 2 vCPU / 4GB RAM / 80GB SSD**. Most providers bill hourly, so spinning it up
for a day or two of testing and destroying it afterward costs well under $2.

If you want to load-test with k6 at meaningful concurrency, bump to 4 vCPU / 8GB.

**Remember to destroy the instance when done testing** -- these bill by the hour/month
until you do.

## 2. Provision + first login

1. Create the VPS with Ubuntu 22.04, note the public IP.
2. `ssh root@<ip>`
3. Install Docker + Compose plugin:
   ```
   curl -fsSL https://get.docker.com | sh
   apt install -y docker-compose-plugin
   ```

## 3. Get the code onto the server

```
git clone <your repo url> imaginv4
cd imaginv4/backend
cp .env.example .env
nano .env   # fill in real values -- see step 4
```

(If the repo isn't pushed anywhere yet, `scp -r backend root@<ip>:~/imaginv4-backend`
from your machine works too.)

## 4. Edit `.env` for the test server

Minimum changes from the template:
- `APP_CORS_ALLOW_ORIGINS_CSV` -- add whatever origin your หัวหน้า will call from (a
  Postman request doesn't need this, but a browser-based frontend does).
- `APP_COOKIE_SECURE=false` unless you're putting HTTPS in front (see step 7) -- with
  `true` and plain HTTP, session cookies won't be set at all. Postman's
  `X-Session-Token` header auth (see `backend/docs/api-quick-reference.md`) works
  either way and is the simpler option for a boss just poking at the API.
- `APP_COMFY_MODE=mock` if this test is purely about the API/backend under load, not
  real image output -- avoids needing your DGX Spark reachable from the VPS at all. Set
  it to `live` + `APP_COMFY_BASE_URL=http://<dgx-spark-ip>:8188` only if you specifically
  want real generations too (requires the VPS to reach the DGX Spark over the network --
  VPN/port-forward, it won't reach a home/office IP by default).
- `APP_GEMINI_API_KEY` -- only if poster/infographic generation should be tested for
  real (this spends real money on every request now that there's no confirmation step --
  see the "removed the confirm gate" change).

## 5. Firewall -- don't expose the datastores publicly

`docker-compose.yml` maps Postgres/Redis/MinIO ports to the host for local dev
convenience. On a public VPS, lock those down:

```
ufw allow 22/tcp        # SSH
ufw allow 8000/tcp       # the API -- what your หัวหน้า actually needs to reach
ufw enable
```

This leaves 5432/6432/6379/9000/9001/8188 unreachable from outside even though
docker-compose still binds them to the host -- only SSH and the API are open.

## 6. Bring the stack up

```
cd ~/imaginv4/backend
docker compose up -d postgres pgbouncer redis minio minio-init mock-comfyui
docker compose run --rm api alembic upgrade head
docker compose up -d api scheduler reconciler
```

## 7. Verify

```
curl http://<ip>:8000/v1/health/live
```

Give your หัวหน้า `http://<ip>:8000` as the `base_url` in the Postman collection
(`backend/docs/postman/Imaginv4.postman_collection.json` -- just edit the
`base_url` collection variable).

### Optional: a real HTTPS URL instead of `http://<ip>:8000`

If your หัวหน้า wants something cleaner (or `APP_COOKIE_SECURE=true` for realistic
cookie-based testing), point a domain/subdomain at the VPS and run a reverse proxy with
automatic TLS in front of port 8000, e.g. Caddy:

```
apt install -y caddy
```
`/etc/caddy/Caddyfile`:
```
test-api.yourdomain.com {
    reverse_proxy localhost:8000
}
```
```
systemctl restart caddy
```
Then use `https://test-api.yourdomain.com` as the base URL and set
`APP_COOKIE_SECURE=true` in `.env`.

## 7b. Minting an API key for an external caller (e.g. the UTCC workflow)

Once migrations are applied (step 6 runs `alembic upgrade head`, which includes the
`api_keys` table), create a dedicated service account and print its key:

```
docker compose run --rm api python -m scripts.create_api_key \
    --email utcc-agent@service.internal --label "UTCC agent workflow"
```

Copy the printed raw key immediately (it's shown once) into the caller's config as
`Authorization: Bearer <key>`. That caller should call `POST /v1/agent/message` (see
`backend/docs/api-quick-reference.md`), not `smart-message` -- it doesn't need to create
a conversation itself, just pass its own per-end-user id as `external_conversation_id`.

## 8. Tear down after testing

```
docker compose down -v   # -v also drops the Postgres/Redis/MinIO volumes
```
Then destroy the VPS from your provider's dashboard so billing stops.
