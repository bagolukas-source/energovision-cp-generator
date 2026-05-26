**To:** `solar.api@fronius.com` (alt. cez `solarweb.com` → Account → API)
**From:** lukas.bago@energovision.sk
**Subject:** Solar.web API v2 access — Service Provider (Energovision)

---

Hello Fronius Solar.web API team,

We are **Energovision s.r.o.**, a Slovak EPC company maintaining a fleet of
Fronius Primo, Symo and Galvo inverters installed at residential and commercial
customers across Slovakia.

We are building an internal multi-vendor monitoring platform and would like to
request Solar.web API v2 access at **Service Provider tier** so that we can
aggregate fleet-wide telemetry, alarms and KPI for our customer portfolio.

Specifically we need:

1. **OAuth2 credentials** — `client_id` + `client_secret`
2. **AccessKeyId + AccessKeyValue** for our Service Provider account
3. **Read endpoints:**
   - `/swqapi/pvsystems-list` — list of all PV systems under our Service Provider rights
   - `/swqapi/pvsystems/{id}/aggdata` — aggregated production data
   - `/swqapi/pvsystems/{id}/devices` — device metadata
   - `/swqapi/pvsystems/{id}/messages` — alarms / events
4. **Push webhook** capability for real-time alarm notifications (preferred over polling)
5. **Rate limit** specification
6. **Documentation** for v2 schema and authentication flow

Energovision details:
- Legal name: Energovision s.r.o.
- IČO: <doplň>
- IČ DPH: SK2121238526
- Service Provider account: `<account_email>`
- Fleet: ~<XX> Fronius systems (<YY> kWp)
- Country: Slovakia

Could you confirm what documentation is required (NDA, commercial agreement,
fleet declaration) and the timeline for credential issuance?

Best regards,
Lukáš Bago
Technical Director, Energovision s.r.o.
lukas.bago@energovision.sk
+421 …
