-- AOM: atómové nahradenie výsledku behu matice (audit 2026-10-24 A10/F14, návrh z Codex 1.0.0rc1).
--
-- Doteraz run_variants_premium robil DELETE variantov → INSERT nových → UPDATE analyza_om
-- tromi samostatnými volaniami. Pád medzi nimi nechal analýzu bez variantov alebo s variantmi,
-- ku ktorým full_response nesedí. Táto funkcia urobí všetko v jednej transakcii.
--
-- Rozdiely oproti rc1 (prispôsobené súčasnému správaniu a schéme):
--   * ručné varianty (capex_source='manual') sa NEzachovávajú v DB, ale prichádzajú v p_rows
--     prepočítané enginom — ich id je vopred pridelené v Pythone, takže full_response.variants[].db_id
--     sedí s riadkom už pri zápise;
--   * výber (selected_variant_id) premapuje Python, funkcia ho len overí (musí patriť analýze);
--   * chránené riadky (is_protected) DELETE potichu preskočí trigger analyza_om_variants_chran_delete
--     — rovnako ako doteraz;
--   * p_expected_updated_at je voliteľný (NULL = bez kontroly súbehu).
--
-- Kým migrácia nie je aplikovaná, run_variants_premium použije pôvodný postup (fallback).
begin;

create or replace function public.aom_replace_engine_result(
    p_analyza_id uuid,
    p_expected_updated_at timestamptz,
    p_rows jsonb,
    p_sim_results jsonb,
    p_econ_results jsonb,
    p_selected_variant_id uuid
) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
    current_record public.analyza_om%rowtype;
    new_selected uuid;
    inserted integer;
begin
    if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) = 0 then
        raise exception 'AOM: prázdny výsledok — nič sa nenahrádza';
    end if;
    select * into strict current_record from public.analyza_om
        where id = p_analyza_id for update;
    if p_expected_updated_at is not null
       and current_record.updated_at is distinct from p_expected_updated_at then
        raise exception 'AOM: analýza sa počas výpočtu zmenila, spusti prepočet znova'
            using errcode = '40001';
    end if;

    delete from public.analyza_om_variants where analyza_id = p_analyza_id;

    insert into public.analyza_om_variants (
        id, analyza_id, name, position, fve_kwp, fve_tilt_deg, fve_azimuth_deg, fve_topology,
        bess_kwh, bess_kw, bess_arbitrage_enabled, capex_eur, capex_source, inverter_kw,
        result_samosp_pct, result_samostat_pct, result_export_mwh, result_import_mwh,
        result_vyroba_mwh, result_npv_eur_base, result_irr_pct_base, result_payback_y_base,
        result_dotacia_eur
    )
    select coalesce(r.id, gen_random_uuid()), p_analyza_id, r.name, coalesce(r.position, e.ord::integer),
        r.fve_kwp, coalesce(r.fve_tilt_deg, 25), coalesce(r.fve_azimuth_deg, 0), coalesce(r.fve_topology, 'south'),
        coalesce(r.bess_kwh, 0), coalesce(r.bess_kw, 0), coalesce(r.bess_arbitrage_enabled, false),
        r.capex_eur, r.capex_source, r.inverter_kw,
        r.result_samosp_pct, r.result_samostat_pct, r.result_export_mwh, r.result_import_mwh,
        r.result_vyroba_mwh, r.result_npv_eur_base, r.result_irr_pct_base, r.result_payback_y_base,
        r.result_dotacia_eur
    from jsonb_array_elements(p_rows) with ordinality e(value, ord)
    cross join lateral jsonb_populate_record(null::public.analyza_om_variants, e.value) r;
    get diagnostics inserted = row_count;

    if p_selected_variant_id is not null then
        select id into new_selected from public.analyza_om_variants
            where id = p_selected_variant_id and analyza_id = p_analyza_id;
    end if;

    update public.analyza_om set
        status = 'completed',
        selected_variant_id = new_selected,
        sim_results = p_sim_results,
        econ_results = p_econ_results,
        updated_at = clock_timestamp()
    where id = p_analyza_id;

    return jsonb_build_object('ok', true, 'variants_count', inserted, 'selected_variant_id', new_selected);
end;
$$;

revoke all on function public.aom_replace_engine_result(uuid, timestamptz, jsonb, jsonb, jsonb, uuid)
    from public, anon, authenticated;
grant execute on function public.aom_replace_engine_result(uuid, timestamptz, jsonb, jsonb, jsonb, uuid)
    to service_role;

commit;
