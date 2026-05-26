**To:** `api@solinteg.com` (alt. cez Solinteg support portál → API access request)
**From:** lukas.bago@energovision.sk
**Subject:** API access request — Energovision (installer account)

---

Hello Solinteg API team,

We are **Energovision s.r.o.** — a Slovak EPC company installing and maintaining
Solinteg MHT-10K-25 hybrid inverters and EBA battery systems for our residential
and commercial customers (currently ~400 systems across Slovakia, including a
growing share of Solinteg installations).

We are building an internal multi-vendor monitoring platform that aggregates
real-time and historical data across our customer portfolio (Huawei, Solinteg,
GoodWe, Fronius, Sungrow). This platform powers our internal dispatch center,
predictive maintenance, customer-facing portals and compliance checks (zero-export
verification, voltage/frequency excursions).

To integrate Solinteg, we need installer-level OpenAPI access. Specifically:

1. **OAuth2 client credentials** (client_id + client_secret) tied to our installer
   account `<installer_username>`
2. **Read scope** for the following endpoints:
   - `/v1/plant/list/byInstaller`
   - `/v1/plant/realtime`
   - `/v1/plant/dailyKpi`
   - `/v1/plant/alarmList`
3. **Write scope (later phase)** for `/v1/inverter/sendCommand` to enable remote
   curtailment when negative day-ahead spot prices trigger our SPOT reactor logic
4. **Rate limit specification** (req/hour per account)
5. **Webhook capability** if available — to receive alarm push notifications
6. **Sandbox/test environment** if available, for development testing

Energovision business details:
- Legal name: Energovision s.r.o.
- IČO: <doplň>
- IČ DPH: SK2121238526
- Installed Solinteg fleet: <X kWp / Y inštalácií>
- Technical contact: Lukáš Bago, lukas.bago@energovision.sk, +421 …

Could you please confirm what documentation we need to submit (NDA, business
verification, technical contact form) and provide a timeline for credential
issuance?

We are happy to sign any data-processing or NDA agreements required.

Best regards,
Lukáš Bago
Energovision s.r.o.
lukas.bago@energovision.sk
+421 …
www.energovision.sk
