**To:** GoodWe Service Provider portál (`semsportal.com` → Support → Developer / API)
**Alt:** `service.eu@goodwe.com`
**From:** lukas.bago@energovision.sk
**Subject:** SEMS Portal API access — Service Provider request

---

Hello GoodWe SEMS team,

We are **Energovision s.r.o.**, Slovak EPC installer of GoodWe GW-ET hybrid
inverters (GW6000-ET, GW8000-ET, GW10K-ET) and Pylontech battery systems for
residential and commercial customers.

We operate a Service Provider account on SEMS Portal (`<installer_username>`) and
manage approximately **<XX>** GoodWe installations across Slovakia.

We are integrating SEMS Portal into our internal multi-vendor monitoring and
dispatch platform and would like to request developer API access.

Specifically we need:

1. **CrossLogin token endpoint** access for our Service Provider account
2. **Read endpoints:**
   - `/api/v3/PowerStation/QueryPagedPlantList` — installer fleet list
   - `/api/v3/PowerStation/GetPowerStationByDetail` — realtime + per-plant detail
   - `/api/v3/Monitor/GetWarningStation` — alarms
   - `/api/v3/PowerStation/GetPlantPowerChart` — daily/historical
3. **Rate limit specification** (req/hour per token)
4. **API documentation** including request/response schemas
5. **Webhook / push capability** for alarm events (optional)

Service Provider details:
- SEMS account: `<installer_username>`
- Company: Energovision s.r.o., IČO `<doplň>`, IČ DPH SK2121238526
- Country: Slovakia
- Fleet size: <XX> GoodWe inverters (~<YY> kWp)

Could you confirm:
- Which API tier covers our use case (read + alarms, no commands required)
- Whether NDA or commercial agreement is needed
- Expected timeline for credential issuance

Thank you,
Lukáš Bago
Energovision s.r.o.
lukas.bago@energovision.sk
+421 …
