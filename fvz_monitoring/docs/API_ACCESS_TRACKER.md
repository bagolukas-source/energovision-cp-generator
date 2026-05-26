# Vendor API Access Tracker — FVZ Monitoring AI

Status žiadostí o installer-level cloud API prístup pre každého vendora.

| Vendor | Status | Žiadosť odoslaná | Schválené | Endpoint | Sandbox testovaný | Live ingest |
| --- | --- | --- | --- | --- | --- | --- |
| **Huawei** FusionSolar | 🟢 LIVE (read-only) | — | áno (Basic API) | `intl.fusionsolar.huawei.com` | áno | áno (SPOT reactor) |
| Huawei sendCommand (SP scope) | 🟡 PENDING | TODO Lukáš | nie | — | — | dry_run mode |
| **Solinteg** iSolar | ⏳ ČAKÁ | TODO | — | `api.solinteg-cloud.com` | — | — |
| **GoodWe** SEMS | ⏳ ČAKÁ | TODO | — | `www.semsportal.com` | — | — |
| **Fronius** Solar.web | ⏳ ČAKÁ | TODO | — | `api.solarweb.com` | — | — |
| **Sungrow** iSolarCloud | ⏳ ČAKÁ | TODO | — | `gateway.isolarcloud.eu` | — | — |

## Čo treba získať od každého vendora

### Univerzálne požiadavky
1. **Installer-level account** (jeden účet vidí všetkých zákazníkov pod inštalátorom Energovision)
2. **OAuth2 client_id + client_secret** ALEBO API key + secret
3. **Read scope:**
   - Plant list (zoznam staníc)
   - Real-time data (AC power, energy, battery SoC, atď.)
   - Daily/historical KPI
   - Alarm list
4. **Write scope (voliteľné, Phase 3):**
   - Inverter command (set_active_power_limit, restart)
5. **Webhook capabilities** (voliteľné) — push notifikácie pri alarmoch
6. **Rate limits** — koľko requestov/hodinu/účet (potrebné na konfiguráciu polling-u)
7. **Sandbox/test prostredie** (ak existuje)

### Vendor-specific

**Solinteg:**
- iSolar OpenAPI access — kontakt `api@solinteg.com` alebo cez ich support portál
- OAuth2 client_credentials flow
- Potrebné: `client_id`, `client_secret`, installer username/password

**GoodWe:**
- SEMS Portal Developer API — formulár cez `developer.semsportal.com` (ak existuje, inak email support)
- Špecificky pýtať **Service Provider scope** (read + alarms)
- Token je obyčajný — username/password installer účtu

**Fronius:**
- Solar.web API v2 — registrácia na `www.solarweb.com` → Developer section
- Vyžaduje: `client_id`, `client_secret`, `AccessKeyId`, `AccessKeyValue`
- Service Provider rola pre prístup ku všetkým zákazníckym systémom

**Sungrow:**
- iSolarCloud OpenAPI — kontakt cez `eu.support@sungrow.com` alebo lokálny SK distribútor
- Vyžaduje: `app_key`, installer username/password (MD5 hashed)
- EU región: `gateway.isolarcloud.eu`

## Šablóny žiadostí

Pre každého vendora je v tomto priečinku samostatný `.md` súbor s pripraveným textom emailu, ktorý vieš poslať (po doplnení Tvojich osobných údajov).

- [Solinteg](./solinteg_email.md)
- [GoodWe](./goodwe_email.md)
- [Fronius](./fronius_email.md)
- [Sungrow](./sungrow_email.md)

## Update procedure

Keď príde odpoveď od vendora, aktualizuj túto tabuľku:
- ⏳ ČAKÁ → 🟡 PENDING (žiadosť potvrdená)
- 🟡 PENDING → 🟢 LIVE (credentials prijaté)

Aktualizuj aj `inverter_vendor_credentials` tabuľku v Supabase a prepni `VENDOR_X_ENABLED=true` v Render env.
