# Sungrow iSolarCloud OpenAPI — request email

**To:** `service@sungrow-emea.com`
**Cc:** `developers@sungrowpower.com` (vývojársky tím Sungrow)
**Subject:** iSolarCloud OpenAPI access — Energovision s.r.o. (EU Service Provider, ~XX inverterov)

---

Dear Sungrow EMEA team,

We are **Energovision s.r.o.**, a Slovak EPC integrator and certified installer
of Sungrow inverters. We operate a Service Provider account on iSolarCloud EU
(`gateway.isolarcloud.eu`) and currently maintain **~XX Sungrow inverters
(~YY kWp)** for our residential and commercial customers across Slovakia.

We are building an **internal multi-vendor monitoring and dispatch platform**
that integrates with our existing Slovak day-ahead spot market control system
(automatic curtailment at negative DAM prices via OKTE — already live for our
Huawei fleet). To extend this dispatch capability and our predictive maintenance
analytics to our Sungrow fleet, we need **installer-level OpenAPI access**.

## Specifically we need

### Endpoints (read scope, Phase 1)
- `POST /openapi/login` — token issuance for our installer account
- `POST /openapi/getPowerStationList` — fleet listing under installer
- `POST /openapi/getStationRealKpi` — batch real-time KPI (we will poll every 5 min)
- `POST /openapi/queryDeviceList` — device-level inventory
- `POST /openapi/queryAlarmInfoList` — alarms feed
- `POST /openapi/getPowerStationDay` — daily summaries

### Endpoints (write scope, Phase 2)
- `POST /openapi/setDeviceParam` — for active power limit control (SPOT
  curtailment use case — already operational on Huawei via similar flow)

### Operational specs
- **appkey + x-access-key** for our installer account `<doplň_installer_username>`
- **Rate limit per appkey** (req/hour, burst limit) — to design polling cadence
- **Full parameter dictionary** (p83022, p83025, p83102, p84xxx series for hybrid
  inverters and BESS systems)
- **Push webhook capability** if available — preferred over polling for alarm
  notifications

## Energovision business details

- Legal: Energovision s.r.o.
- IČ DPH: SK2121238526
- IČO: `<doplň>`
- Address: `<doplň>`, Slovakia
- iSolarCloud EU region: `gateway.isolarcloud.eu`
- Sungrow Service Provider account: `<doplň_installer_username>`
- Installed Sungrow fleet: ~`<XX>` inverters (~`<YY>` kWp)
- Inverter models in our portfolio: SG6.0RT, SG8.0RT, SG10RT, SH5.0RT-V11, ...
- Battery models: SBR064, SBR096, SBR128, SBR192
- Technical contact: Lukáš Bago, Technical Director, lukas.bago@energovision.sk

## Why this matters for Sungrow

By granting us OpenAPI access you enable:
1. **Faster fault detection** — our predictive maintenance reduces customer
   downtime and service tickets reaching Sungrow's L2 support
2. **Reference case for Slovak DAM curtailment** — we already operate a 21-station
   Huawei fleet under negative-price curtailment with auto-shutdown logic. Sungrow
   would be the **second vendor** in our reference implementation. Happy to share
   findings publicly with your permission.
3. **Pipeline growth** — our Sungrow installation pipeline is `<XX>` inverters
   over next 12 months; reliable monitoring is a key vendor selection criterion
   for our customers

## Next steps

Please advise:

1. Whether OpenAPI access is granted **at no charge** at installer level (vs.
   a separate commercial API tier we'd need to subscribe to)
2. **What documentation we need to submit** — NDA, data processing agreement,
   technical contact form, fleet declaration
3. **Expected timeline** for credential issuance
4. **Test/sandbox environment** if available for development

We are ready to sign NDA / DPA agreements immediately.

Thank you for your support — we believe deeper integration will benefit both
our customers and Sungrow's footprint in the Slovak market.

Best regards,

**Lukáš Bago**
Technical Director
Energovision s.r.o.
lukas.bago@energovision.sk
+421 `<doplň>`
www.energovision.sk

---

## Slovenská verzia (ak by si chcel poslať aj SK distribútorovi)

Predmet: **Žiadosť o OpenAPI prístup k iSolarCloud — Energovision s.r.o.**

Dobrý deň,

sme spoločnosť **Energovision s.r.o.**, slovenský EPC inštalátor a certifikovaný
partner Sungrow. Prevádzkujeme Service Provider účet na iSolarCloud EU
a aktuálne spravujeme cca **<XX> Sungrow meničov (<YY> kWp)** pre rezidenčných
a komerčných zákazníkov v SR.

Staviame interný multi-vendor monitoring a dispečing, ktorý integruje aj
automatickú reguláciu pri záporných cenách OKTE day-ahead (už produkčne beží
pre náš Huawei fleet — 21 staníc).

Pre rozšírenie na Sungrow potrebujeme **installer-level OpenAPI**:

- appkey + token endpoint pre installer účet
- read scope: getPowerStationList, getStationRealKpi, queryAlarmInfoList,
  getPowerStationDay
- write scope (neskôr, fáza 2): setDeviceParam pre active power limit
- rate limit a parameter dictionary (kódy p83xxx, p84xxx)

Prosím o info aký je proces:
- Či je OpenAPI bez poplatku pri installer účte
- Aké dokumenty (NDA, DPA, business verifikácia) potrebujete
- Časový rámec na vydanie credentials

Sme pripravení podpísať akúkoľvek zmluvnú dokumentáciu obratom.

Ďakujem,

**Lukáš Bago**
Technický riaditeľ
Energovision s.r.o.
lukas.bago@energovision.sk
+421 `<doplň>`
