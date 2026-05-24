// Posudok 1 — RE-PLAST, s.r.o. — FVE + BESS spolu (P-26-134)
const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
  Header, Footer, AlignmentType, LevelFormat,
  TabStopType, HeadingLevel, BorderStyle, WidthType, ShadingType,
  VerticalAlign, PageNumber, SectionType
} = require('docx');

const DIR = __dirname;
const RES = JSON.parse(fs.readFileSync(path.join(DIR, 'final_results.json'), 'utf-8'));

const EV_GREEN = '92D050', EV_GREEN_LIGHT = 'E8F4D5';
const EV_BLACK = '1A1A1A', EV_DARK = '2C2C2C', EV_GRAY = '8C8C8C';
const EV_LIGHTGRAY = 'F5F5F5', EV_BORDER = 'D9D9D9';
const EV_RED = 'E74C3C', EV_ORANGE = 'F39200', EV_BLUE = '0F4C81';

const t = (text, opts = {}) => new TextRun({ text, font: 'Arial', ...opts });
const kicker = (text) => new Paragraph({ spacing: { before: 320, after: 60 },
  children: [new TextRun({ text: text.toUpperCase(), font: 'Arial', size: 18, bold: true, color: EV_GREEN, characterSpacing: 80 })] });
const h1 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 0, after: 240 },
  children: [new TextRun({ text, font: 'Arial', size: 40, bold: true, color: EV_BLACK })] });
const h2 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 280, after: 140 },
  children: [new TextRun({ text, font: 'Arial', size: 26, bold: true, color: EV_BLACK })] });
const h3 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_3, spacing: { before: 200, after: 80 },
  children: [new TextRun({ text, font: 'Arial', size: 22, bold: true, color: EV_DARK })] });
const para = (children, opts = {}) => {
  if (typeof children === 'string') children = [new TextRun({ text: children, font: 'Arial', size: 22, color: EV_DARK })];
  return new Paragraph({ children, spacing: { after: 140 }, ...opts });
};
const bullet = (children) => {
  if (typeof children === 'string') children = [new TextRun({ text: children, font: 'Arial', size: 22, color: EV_DARK })];
  return new Paragraph({ numbering: { reference: 'green-bullets', level: 0 }, children, spacing: { after: 80 } });
};
const cellBorder = { style: BorderStyle.SINGLE, size: 4, color: EV_BORDER };
const noBorder = { style: BorderStyle.NONE, size: 0, color: 'FFFFFF' };
const cellBorders = { top: cellBorder, bottom: cellBorder, left: cellBorder, right: cellBorder };

function cell(text, opts = {}) {
  const { width, bold = false, fill, align = AlignmentType.LEFT, color = EV_DARK, size = 20, italic = false, borders } = opts;
  return new TableCell({
    width: { size: width, type: WidthType.DXA }, borders: borders || cellBorders,
    margins: { top: 120, bottom: 120, left: 140, right: 140 },
    shading: fill ? { fill, type: ShadingType.CLEAR } : undefined,
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ alignment: align, children: typeof text === 'string'
      ? [new TextRun({ text, font: 'Arial', bold, color, size, italics: italic })] : text })]
  });
}
function dataTable(rows, columnWidths) {
  const totalWidth = columnWidths.reduce((a, b) => a + b, 0);
  const normalizedRows = rows.map(r => {
    if (Array.isArray(r)) return r;
    const arr = []; for (let k = 0; k < columnWidths.length; k++) arr.push(r[k]); return arr;
  });
  return new Table({
    width: { size: totalWidth, type: WidthType.DXA }, columnWidths,
    rows: normalizedRows.map((r, i) => new TableRow({
      children: r.map((c, j) => {
        const isHeader = i === 0;
        if (typeof c === 'object' && c !== null && 'text' in c) return cell(c.text, { width: columnWidths[j], ...c });
        return cell(c, {
          width: columnWidths[j], bold: isHeader,
          fill: isHeader ? EV_BLACK : (i % 2 === 0 ? EV_LIGHTGRAY : 'FFFFFF'),
          color: isHeader ? 'FFFFFF' : EV_DARK,
          align: j === 0 ? AlignmentType.LEFT : AlignmentType.CENTER,
        });
      })
    }))
  });
}
function img(filename, w, h, align = AlignmentType.CENTER) {
  return new Paragraph({ alignment: align, spacing: { before: 120, after: 120 },
    children: [new ImageRun({ type: 'png', data: fs.readFileSync(path.join(DIR, filename)),
      transformation: { width: w, height: h },
      altText: { title: 'Image', description: 'Image', name: 'Image' } })] });
}
function caption(text) {
  return new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 240 },
    children: [new TextRun({ text, font: 'Arial', italics: true, size: 18, color: EV_GRAY })] });
}
function highlightBox(kickerText, paragraphs, color = EV_GREEN) {
  const innerCells = [new Paragraph({ spacing: { before: 0, after: 100 },
    children: [new TextRun({ text: kickerText.toUpperCase(), font: 'Arial', size: 18, bold: true, color, characterSpacing: 80 })] })];
  paragraphs.forEach(par => innerCells.push(par));
  return new Table({
    width: { size: 9026, type: WidthType.DXA }, columnWidths: [9026],
    rows: [new TableRow({ children: [new TableCell({
      width: { size: 9026, type: WidthType.DXA },
      borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 32, color } },
      shading: { fill: EV_LIGHTGRAY, type: ShadingType.CLEAR },
      margins: { top: 240, bottom: 240, left: 280, right: 280 },
      children: innerCells
    })], cantSplit: true })]
  });
}
function stepsTable(steps) {
  const widths = [3009, 3009, 3008];
  const titleRowCells = steps.map((s, i) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 24, color: EV_GREEN } },
    margins: { top: 200, bottom: 80, left: 240, right: 200 },
    children: [
      new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: String(i+1).padStart(2,'0'), font: 'Arial', size: 36, bold: true, color: EV_GREEN })] }),
      new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: s.title, font: 'Arial', size: 22, bold: true, color: EV_BLACK })] }),
      new Paragraph({ spacing: { after: 240 }, children: [new TextRun({ text: s.body, font: 'Arial', size: 18, color: EV_DARK })] })
    ]
  }));
  return new Table({ width: { size: 9026, type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ children: titleRowCells, cantSplit: true })] });
}
function kpiBox(items) {
  const widths = items.length === 4 ? [2256, 2256, 2257, 2257] : [3009, 3009, 3008];
  return new Table({
    width: { size: 9026, type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ children: items.map((it, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA },
      borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 24, color: EV_GREEN } },
      shading: { fill: EV_LIGHTGRAY, type: ShadingType.CLEAR },
      margins: { top: 180, bottom: 180, left: 220, right: 200 },
      children: [
        new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: it.label.toUpperCase(), font: 'Arial', size: 14, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
        new Paragraph({ spacing: { after: 40 }, children: [new TextRun({ text: it.value, font: 'Arial', size: 28, bold: true, color: EV_BLACK })] }),
        new Paragraph({ spacing: { after: 0 }, children: [new TextRun({ text: it.note || '', font: 'Arial', size: 16, italics: true, color: EV_GRAY })] }),
      ]
    })) }), ]
  });
}
const fmt = (n, dec = 1) => n.toLocaleString('sk-SK', { minimumFractionDigits: dec, maximumFractionDigits: dec });
const fmt0 = (n) => n.toLocaleString('sk-SK', { minimumFractionDigits: 0, maximumFractionDigits: 0 });

const C = RES.common, P1 = RES.p1_combo, CAP = RES.capex;
const P1B = P1['Báza'], P1N = P1['Nízky výkup'], P1S = P1['Spot s arbitrážou (BS plne aktívna)'];

const doc = new Document({
  creator: 'Energovision', title: 'Posudok FVE + BESS — RE-PLAST',
  styles: { default: { document: { run: { font: 'Arial', size: 22, color: EV_DARK } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 40, bold: true, font: 'Arial', color: EV_BLACK },
        paragraph: { spacing: { before: 0, after: 240 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 26, bold: true, font: 'Arial', color: EV_BLACK },
        paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 1 } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 22, bold: true, font: 'Arial', color: EV_DARK },
        paragraph: { spacing: { before: 200, after: 80 }, outlineLevel: 2 } },
    ] },
  numbering: { config: [{ reference: 'green-bullets',
    levels: [{ level: 0, format: LevelFormat.BULLET, text: '●', alignment: AlignmentType.LEFT,
      style: { run: { color: EV_GREEN, font: 'Arial' }, paragraph: { indent: { left: 540, hanging: 270 } } } }] }] },
  sections: [
    // TITULNÁ
    { properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } } },
      headers: { default: new Header({ children: [para('')] }) },
      footers: { default: new Footer({ children: [para('')] }) },
      children: [
        img('logo.png', 360, 72, AlignmentType.RIGHT),
        new Paragraph({ spacing: { before: 480, after: 80 },
          children: [
            new TextRun({ text: 'TECHNICKO-EKONOMICKÝ POSUDOK', font: 'Arial', size: 20, bold: true, color: EV_GREEN, characterSpacing: 80 }),
            new TextRun({ text: '   ·   ', font: 'Arial', size: 20, color: EV_GRAY }),
            new TextRun({ text: 'P-26-134 / Posudok 1 — FVE + BESS', font: 'Arial', size: 20, color: EV_DARK })
          ] }),
        new Paragraph({ spacing: { after: 100 },
          children: [new TextRun({ text: 'RE-PLAST, s.r.o.', font: 'Arial', size: 56, bold: true, color: EV_BLACK })] }),
        para([new TextRun({ text: 'Hybridné energetické riešenie — fotovoltika 1 200 kWp + batériové úložisko 1 205 kWh', font: 'Arial', size: 26, color: EV_DARK })]),
        new Paragraph({ spacing: { after: 600 },
          children: [new TextRun({ text: 'Vlastná výroba elektriny + arbitráž bilančnej skupiny — od projektu po dispatch.', font: 'Arial', italics: true, size: 22, color: EV_GRAY })] }),
        new Table({
          width: { size: 9746, type: WidthType.DXA }, columnWidths: [4873, 4873],
          rows: [new TableRow({ children: [
            new TableCell({
              width: { size: 4873, type: WidthType.DXA },
              borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 24, color: EV_GREEN } },
              margins: { top: 100, bottom: 100, left: 240, right: 200 },
              children: [
                new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: 'PRE', font: 'Arial', size: 16, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
                new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: 'RE-PLAST, s.r.o.', font: 'Arial', size: 22, bold: true, color: EV_BLACK })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'Zvončín 107', font: 'Arial', size: 18, color: EV_DARK })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'Zvončín, Slovenská republika', font: 'Arial', size: 18, color: EV_DARK })] }),
                new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: 'Kontakt: Ján Krčula', font: 'Arial', size: 18, italics: true, color: EV_DARK })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'PARAMETRE OM', font: 'Arial', size: 14, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'MRK: 2 200 kW · max prietok DS: 1 850 kW', font: 'Arial', size: 16, color: EV_DARK })] }),
                new Paragraph({ spacing: { after: 0 }, children: [new TextRun({ text: 'Spotreba: 8 045 MWh/rok (VN)', font: 'Arial', size: 16, color: EV_DARK })] }),
              ] }),
            new TableCell({
              width: { size: 4873, type: WidthType.DXA },
              borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 24, color: EV_GREEN } },
              margins: { top: 100, bottom: 100, left: 240, right: 200 },
              children: [
                new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: 'VYSTAVENÉ', font: 'Arial', size: 16, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
                new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: '06.05.2026', font: 'Arial', size: 22, bold: true, color: EV_BLACK })] }),
                new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: 'Bratislava', font: 'Arial', size: 18, italics: true, color: EV_DARK })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'POSUDZOVANÉ OBDOBIE', font: 'Arial', size: 14, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
                new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: '01.01.2025 – 31.12.2025', font: 'Arial', size: 20, bold: true, color: EV_BLACK })] }),
                new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'NADVÄZUJÚCA PONUKA', font: 'Arial', size: 14, bold: true, color: EV_GRAY, characterSpacing: 80 })] }),
                new Paragraph({ spacing: { after: 0 }, children: [new TextRun({ text: 'PON-26-264 (FVE + Huawei BESS)', font: 'Arial', size: 16, color: EV_DARK })] }),
              ] }),
          ]})]
        }),
        new Paragraph({ spacing: { before: 600 }, children: [t('')] }),
        new Table({
          width: { size: 9746, type: WidthType.DXA }, columnWidths: [9746],
          rows: [new TableRow({ children: [new TableCell({
            width: { size: 9746, type: WidthType.DXA },
            borders: { top: noBorder, bottom: noBorder, right: noBorder, left: { style: BorderStyle.SINGLE, size: 32, color: EV_GREEN } },
            shading: { fill: EV_GREEN_LIGHT, type: ShadingType.CLEAR },
            margins: { top: 220, bottom: 220, left: 320, right: 320 },
            children: [
              new Paragraph({ spacing: { after: 60 }, children: [new TextRun({ text: 'PRIPRAVIL PRE VÁS', font: 'Arial', size: 16, bold: true, color: EV_BLACK, characterSpacing: 80 })] }),
              new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'Lukáš Bago', font: 'Arial', size: 22, bold: true, color: EV_BLACK })] }),
              new Paragraph({ spacing: { after: 30 }, children: [new TextRun({ text: 'Energovision, s.r.o.', font: 'Arial', size: 18, italics: true, color: EV_DARK })] }),
              new Paragraph({ spacing: { after: 0 }, children: [
                new TextRun({ text: 'lukas.bago@energovision.sk', font: 'Arial', size: 18, color: EV_DARK }),
                new TextRun({ text: '   ·   ', font: 'Arial', size: 18, color: EV_GREEN }),
                new TextRun({ text: '0918 187 762', font: 'Arial', size: 18, color: EV_DARK })
              ]}),
            ] })]})]
        }),
      ] },
    // HLAVNÝ
    { properties: { type: SectionType.NEXT_PAGE, page: { size: { width: 11906, height: 16838 }, margin: { top: 1280, right: 1080, bottom: 1280, left: 1080 } } },
      headers: { default: new Header({ children: [
        new Paragraph({ tabStops: [{ type: TabStopType.RIGHT, position: 9746 }], spacing: { after: 80 },
          children: [
            new TextRun({ text: 'energo', font: 'Arial', size: 20, bold: true, color: EV_BLACK }),
            new TextRun({ text: 'vision', font: 'Arial', size: 20, bold: true, color: EV_GREEN }),
            new TextRun({ text: '\tPosudok 1 · FVE + BESS', font: 'Arial', size: 18, italics: true, color: EV_DARK }),
            new TextRun({ text: '   ·   ', font: 'Arial', size: 18, color: EV_GREEN }),
            new TextRun({ text: 'P-26-134', font: 'Arial', size: 18, color: EV_DARK }),
          ],
          border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: EV_GREEN, space: 4 } }
        })
      ]}) },
      footers: { default: new Footer({ children: [
        new Paragraph({ tabStops: [{ type: TabStopType.RIGHT, position: 9746 }],
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: EV_BORDER, space: 6 } },
          children: [
            new TextRun({ text: 'Energovision, s.r.o.', font: 'Arial', size: 16, color: EV_GRAY }),
            new TextRun({ text: '   ·   ', font: 'Arial', size: 16, color: EV_GREEN }),
            new TextRun({ text: 'IČO: 53 036 280', font: 'Arial', size: 16, color: EV_GRAY }),
            new TextRun({ text: '   ·   ', font: 'Arial', size: 16, color: EV_GREEN }),
            new TextRun({ text: 'www.energovision.sk', font: 'Arial', size: 16, color: EV_GRAY }),
            new TextRun({ text: '\tstrana ', font: 'Arial', size: 16, color: EV_GRAY }),
            new TextRun({ children: [PageNumber.CURRENT], font: 'Arial', size: 16, bold: true, color: EV_BLACK }),
            new TextRun({ text: ' / ', font: 'Arial', size: 16, color: EV_GRAY }),
            new TextRun({ children: [PageNumber.TOTAL_PAGES], font: 'Arial', size: 16, bold: true, color: EV_BLACK }),
          ]})
      ]}) },
      children: [
        kicker('Manažérske zhrnutie'),
        h1('Hybridný projekt FVE 1 200 kWp + BESS 1 205 kWh dosahuje návratnosť 4,7 roka pri NPV 20 r. +1,8 mil. €.'),
        para('Posudok hodnotí kombinovaný projekt fotovoltickej elektrárne 1 200 kWp v orientácii Východ-Západ a batériového úložiska 1 205 kWh / 540 kW (Huawei LUNA2000-241-2S1, 7 ks) na odbernom mieste spoločnosti RE-PLAST, s.r.o. Klient prevádzkuje výrobnú linku v 24/7 režime so spotrebou 8 045 MWh/rok a max. odberom 1 719 kW (MRK 2 200 kW). Plochý profil odberu (priemer 919 kW celoročne, víkendová prevádzka rovnaká ako pracovné dni) je mimoriadne výhodný pre maximalizáciu samospotreby FVE.'),
        para('Batériové úložisko je navrhnuté ako súčasť bilančnej skupiny (BS) s aktívnym obchodovaním na denno-trhovom (DAM) trhu OKTE. Týmto spôsobom BESS prináša okrem zlepšenia samospotreby FVE prebytkov aj samostatný výnos z arbitráže ~ 58 000 €/rok.'),

        h2('Kľúčové parametre projektu'),
        kpiBox([
          { label: 'CAPEX celkom', value: fmt0(CAP.combo) + ' €', note: 'bez DPH, turn-key' },
          { label: 'Úspora rok 1', value: fmt0(P1B.saving) + ' €', note: 'Báza' },
          { label: 'Návratnosť', value: fmt(P1B.payback) + ' r', note: 'jednoduchá' },
          { label: 'NPV 20 r.', value: '+' + fmt0(P1B.npv_tax/1000) + ' tis. €', note: 's daň. odpisom' },
        ]),

        h2('Odporúčanie'),
        highlightBox('REALIZÁCIA HYBRIDNÉHO PROJEKTU FVE + BESS', [
          new Paragraph({ spacing: { after: 100 }, children: [new TextRun({ text: 'FVE 1 200 kWp E-W + BESS 1 205 kWh / 540 kW', font: 'Arial', size: 28, bold: true, color: EV_BLACK })] }),
          para([
            t('Pri investícii '),
            t(fmt0(CAP.combo) + ' €', { bold: true, color: EV_BLACK }),
            t(' získa klient ročnú úsporu '),
            t(fmt0(P1B.saving) + ' €', { bold: true, color: EV_BLACK }),
            t(' (kombinácia samospotreby FVE + arbitráže BS), '),
            t('NPV za 20 rokov vrátane daňového odpisu +' + fmt0(P1B.npv_tax) + ' €', { bold: true, color: EV_BLACK }),
            t(' a IRR '),
            t(fmt(P1B.irr) + ' %', { bold: true, color: EV_BLACK }),
            t('. Pri prechode na spotovú tarifu s aktívnou arbitrážou stúpa úspora na '),
            t(fmt0(P1S.saving) + ' €/rok', { bold: true, color: EV_BLACK }),
            t(' a návratnosť klesá na '),
            t(fmt(P1S.payback) + ' roka', { bold: true, color: EV_BLACK }),
            t('.'),
          ]),
        ], EV_GREEN),

        kicker('1 — Vstupné dáta a metodika'),
        h1('Východiská posudku'),
        h3('Charakteristika odberného miesta'),
        dataTable([
          ['Parameter', 'Hodnota'],
          ['Adresa OM', 'Zvončín 107, Zvončín'],
          ['Napäťová úroveň', 'VN (vysoké napätie)'],
          ['MRK (max. rezervovaná kapacita)', '2 200 kW'],
          ['Max. prietok do DS', '1 850 kW'],
          ['Ročná spotreba (2025)', '8 045 MWh'],
          ['Priemerný hodinový odber', '919 kW'],
          ['Maximum hodinového odberu', '1 719 kW'],
          ['Profil prevádzky', '24/7 výroba (víkend ⌀ 960 kW vs pracovný deň ⌀ 902 kW)'],
        ], [4500, 4526]),

        h3('Metodika simulácie'),
        para('Hodinová bilančná simulácia 8 760 hodín roka 2025 s využitím reálneho 15-min profilu odberu z merania ZSDIS:'),
        bullet('Spotreba — interpolovaná z 15-min profilu na hodinové priemery.'),
        bullet('FVE produkcia — PVGIS model pre lokáciu Zvončín, FVE 1 200 kWp E-W tilt, ročný výnos ~ 1 068 kWh/kWp.'),
        bullet('BESS dispatch v dvoch režimoch: (a) self-consumption — zachytenie prebytkov FVE; (b) arbitráž BS — nákup zo siete v lacných hodinách (00–05 h), vybíjanie cez deň alebo predaj v drahších hodinách OTE/OKTE.'),
        bullet('Ekonomika — NPV 20 rokov, diskont 6 %, OPEX 1,5 % CAPEX/rok, degradácia FVE 0,5 %/rok, daňový odpis 6 rokov pri sadzbe DPPO 21 %.'),
        bullet('Tarif — silová cena 0,114 €/kWh + distribučné poplatky VN (TPS+TSS+NJF+straty+spotrebná daň) 0,063 €/kWh = celkom 0,177 €/kWh nákup.'),

        kicker('2 — Profil odberu'),
        h1('Charakteristika spotreby'),
        h3('Hodinový profil — pracovný vs víkend'),
        img('graf_profil_hodinovy.png', 540, 240),
        caption('Priemerný hodinový odber pre pracovný deň, sobotu a nedeľu — 24/7 výrobná prevádzka'),
        para('Profil je mimoriadne plochý — priemerný odber sa pohybuje medzi 850–970 kW v každú hodinu dňa (mierne nižší okolo poludnia 11–14 h, čo je výhodné pre súbeh s FVE výrobou). Víkendový odber je dokonca o 6 % vyšší než pracovné dni — typicky pre kontinuálnu výrobnú prevádzku, kde technologický proces beží bez prerušenia.'),

        h3('Mesačná spotreba'),
        img('graf_mesacna.png', 540, 240),
        caption('Mesačná spotreba (MWh) — rok 2025'),
        para('Sezónnosť je mierna — najnižšia spotreba marec (456 MWh, technologická odstávka), najvyššia november (815 MWh). Ročný priemer 670 MWh/mesiac.'),

        h3('Krivka trvania výkonu (LDC)'),
        img('graf_ldc.png', 540, 240),
        caption('Krivka trvania výkonu — MRK 2 200 kW vs reálne maximum 1 719 kW'),
        para([
          t('Z LDC vyplýva: '),
          t('medián 920 kW', { bold: true, color: EV_BLACK }),
          t(', P95 ~ 1 200 kW, max 1 719 kW. '),
          t('MRK 2 200 kW má ~ 480 kW rezervu', { bold: true, color: EV_BLACK }),
          t(' — prekračovanie nehrozí, peak shaving ako primárna funkcia BESS by mal zanedbateľný prínos.'),
        ]),

        kicker('3 — Návrh hybridného riešenia'),
        h1('Technická konfigurácia'),

        dataTable([
          ['Komponent', 'Špecifikácia', 'Kapacita / výkon'],
          ['FVE — fotovoltická elektráreň', '1 200 kWp, panely v orientácii Východ-Západ (tilt 10°)', '1 282 MWh/rok'],
          ['Meniče FVE', 'String alebo central inverter podľa návrhu', '~ 1 000 kVA'],
          ['BESS — batériové úložisko', 'Huawei LUNA2000-241-2S1 (7 ks)', '1 205 kWh / 540 kW'],
          ['Trafostanica', 'Suchý transformátor', '1 600 kVA'],
          ['Riadiaci systém', 'EMS s integráciou do bilančnej skupiny', '—'],
        ], [3000, 3500, 2526]),

        h3('Prevádzkový model BESS'),
        para('BESS je navrhnuté ako aktívny aktívum bilančnej skupiny — okrem klasickej self-consumption funkcionality (zachytávanie prebytkov FVE pre vybíjanie v hodinách s FVE deficitu) sa zúčastňuje obchodovania na DAM/IDM trhu cez OKTE:'),
        bullet([t('Cez deň: ', { bold: true }), t('FVE pokrýva 92,5 % výroby priamo do prevádzky (vďaka 24/7 profilu). Prebytky 51 MWh/rok ukladá BESS namiesto exportu za nízku výkupnú cenu.', { color: EV_DARK })]),
        bullet([t('V noci: ', { bold: true }), t('BESS sa nabíja zo siete v lacných hodinách OKTE DAM (typicky 0–5 h), vybíja v drahších hodinách (popoludní/večer) — arbitráž generuje ~ 58 000 €/rok pre BS.', { color: EV_DARK })]),
        bullet([t('Bilančná skupina: ', { bold: true }), t('BESS pomáha vyrovnávať odchýlky BS (odchýlky avoid) — v posudku konzervatívne nezahrnuté.', { color: EV_DARK })]),

        h3('Modelový týždenný profil'),
        img('graf_tyzden.png', 600, 360),
        caption('Energetická bilancia pre dva typické týždne (jún + marec) — odber RE-PLAST a výroba FVE 1 200 kWp E-W'),

        kicker('4 — Ekonomické posúdenie'),
        h1('Investícia, úspory a NPV'),
        h3('Investičné náklady (CAPEX)'),
        dataTable([
          ['Položka', 'Suma (bez DPH)'],
          ['FVE 1 200 kWp E-W (turn-key)', fmt0(CAP.fve) + ' €'],
          ['BESS 1 205 kWh / 540 kW (Huawei LUNA + trafo + pripojenie)', fmt0(CAP.bess) + ' €'],
          [{ text: 'CAPEX celkom', bold: true, fill: EV_GREEN_LIGHT }, { text: fmt0(CAP.combo) + ' €', bold: true, fill: EV_GREEN_LIGHT }],
        ], [5500, 3526]),

        h3('Skladba ročného prínosu'),
        dataTable([
          ['Zdroj prínosu', 'MWh/r', 'Hodnota'],
          ['Samospotreba FVE (priamo do prevádzky)', '1 186', fmt0(C.fve_self_use_MWh*1000*0.177) + ' €'],
          ['Výkup prebytku FVE do siete', '96', '~ 5 760 €'],
          ['BESS — dodatočná samospotreba prebytkov FVE', fmt(C.bess_self_use_gain_MWh), fmt0(C.bess_self_use_gain_eur) + ' €'],
          ['BESS — arbitráž bilančnej skupiny (DAM, OKTE spot 2025)', '~ 970 (cyklov)', fmt0(C.arb_bs_eur) + ' €'],
          [{ text: 'Spolu ročne (Báza)', bold: true, fill: EV_GREEN_LIGHT }, { text: '—', fill: EV_GREEN_LIGHT }, { text: fmt0(P1B.saving) + ' €', bold: true, fill: EV_GREEN_LIGHT }],
        ], [4500, 1500, 3026]),

        h3('Cenové scenáre — NPV, IRR, návratnosť'),
        para('Modelujeme tri scenáre podľa typu výkupnej zmluvy a aktivity bilančnej skupiny:'),
        dataTable([
          ['Scenár', 'Úspora rok 1', 'Návratnosť', 'NPV 20 r.', 'NPV s daň. odpisom', 'IRR'],
          [{ text: 'Báza', fill: EV_GREEN_LIGHT }, { text: fmt0(P1B.saving) + ' €', bold: true, fill: EV_GREEN_LIGHT }, { text: fmt(P1B.payback) + ' r', bold: true, fill: EV_GREEN_LIGHT }, { text: '+' + fmt0(P1B.npv) + ' €', bold: true, fill: EV_GREEN_LIGHT }, { text: '+' + fmt0(P1B.npv_tax) + ' €', bold: true, fill: EV_GREEN_LIGHT }, { text: fmt(P1B.irr) + ' %', bold: true, fill: EV_GREEN_LIGHT }],
          ['Nízky výkup (0,03 €/kWh)', fmt0(P1N.saving) + ' €', fmt(P1N.payback) + ' r', '+' + fmt0(P1N.npv) + ' €', '+' + fmt0(P1N.npv_tax) + ' €', fmt(P1N.irr) + ' %'],
          ['Spot s arbitrážou (BS aktívna)', fmt0(P1S.saving) + ' €', fmt(P1S.payback) + ' r', '+' + fmt0(P1S.npv) + ' €', '+' + fmt0(P1S.npv_tax) + ' €', fmt(P1S.irr) + ' %'],
        ], [3500, 1600, 1100, 1300, 1526, 0]),

        h3('Daňový odpis'),
        para('FVE patrí do 6. odpisovej skupiny — povinný lineárny daňový odpis na 6 rokov. BESS sa typicky odpisuje samostatne (1.–4. odpisová skupina podľa kvalifikácie). Pri sadzbe DPPO 21 % to predstavuje pre RE-PLAST výraznú úsporu na dani z príjmu:'),
        para([
          t('Pri CAPEX '),
          t(fmt0(CAP.combo) + ' €', { bold: true, color: EV_BLACK }),
          t(' a sadzbe 21 % je celková daňová úspora '),
          t(fmt0(CAP.combo * 0.21) + ' €', { bold: true, color: EV_BLACK }),
          t(' (priemer ' + fmt0(CAP.combo * 0.21 / 6) + ' €/rok počas prvých 6 rokov).'),
        ]),

        kicker('5 — Zhrnutie a odporúčanie'),
        h1('Záverečné odporúčanie'),
        highlightBox('Odporúčanie: realizovať hybridný projekt v navrhnutej konfigurácii', [
          para([
            t('Projekt FVE 1 200 kWp + BESS 1 205 kWh / 540 kW je '),
            t('vynikajúca investícia s návratnosťou 4,7 roka', { bold: true, color: EV_BLACK }),
            t(' a NPV 20 rokov +1,8 milióna eur (vrátane daňového odpisu). IRR 18,7 % výrazne prevyšuje typické bezrizikové úložky aj firemné cieľové ROI.'),
          ]),
          para([
            t('Profil odberu RE-PLAST je '),
            t('mimoriadne výhodný pre fotovoltiku', { bold: true, color: EV_BLACK }),
            t(' — 24/7 prevádzka znamená že 92,5 % výroby FVE ide priamo do spotreby bez exportu. BESS tento podiel zlepší na 96,5 % a navyše generuje samostatný výnos cez arbitráž v bilančnej skupine.'),
          ]),
        ]),

        h3('Argumenty pre realizáciu'),
        bullet([t('Výnimočná návratnosť ', { color: EV_DARK }), t('4,7 roka', { bold: true, color: EV_BLACK }), t(' — výrazne pod priemerom komerčných FVE+BESS projektov.', { color: EV_DARK })]),
        bullet([t('Stabilný profil odberu ', { color: EV_DARK }), t('92,5 % samospotreba FVE', { bold: true, color: EV_BLACK }), t(' bez BESS, 96,5 % s BESS — minimálny export do siete (slabá závislosť od výkupnej ceny).', { color: EV_DARK })]),
        bullet([t('BESS v bilančnej skupine ', { bold: true, color: EV_BLACK }), t(' generuje samostatný príjem ~ 58 000 €/rok z arbitráže — to je investícia, ktorá pracuje 24/7.', { color: EV_DARK })]),
        bullet([t('Daňová optimalizácia ', { bold: true, color: EV_BLACK }), t(' — celková úspora na DPPO ' + fmt0(CAP.combo * 0.21) + ' € počas prvých 6 rokov.', { color: EV_DARK })]),
        bullet([t('Hedge proti regulačnej zmene a inflácii ', { bold: true, color: EV_BLACK }), t(' — vlastná výroba kryje 16 % spotreby na 25+ rokov.', { color: EV_DARK })]),
        bullet([t('ESG profil a CSRD reporting ', { bold: true, color: EV_BLACK }), t(' — redukcia 320 t CO₂/rok, podklad pre Scope 2 (location/market-based).', { color: EV_DARK })]),

        h2('Ďalšie kroky'),
        stepsTable([
          { title: 'Akceptácia ponuky', body: 'Klient odsúhlasí cenovú ponuku PON-26-264 a parametre hybridného riešenia.' },
          { title: 'Zmluva o dielo', body: 'Pevná cena, harmonogram, míľniky. Vybavenie kladného stanoviska VSDS — administráciu vedie Energovision.' },
          { title: 'Realizácia (4–6 mes.)', body: 'Inžiniering, montáž FVE + BESS + trafostanice, parametrizácia EMS, integrácia do BS, spustenie a monitoring.' },
        ]),

        new Paragraph({ spacing: { before: 480, after: 0 }, alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: 'Energovision — viac než len dodávateľ fotovoltických systémov.', font: 'Arial', italics: true, size: 22, color: EV_GRAY })] }),
        new Paragraph({ spacing: { before: 0, after: 0 }, alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: 'Energetický partner pre budúcnosť.', font: 'Arial', italics: true, bold: true, size: 22, color: EV_BLACK })] }),
      ] }
  ]
});

const OUT_DIR = path.dirname(DIR);
const OUT = path.join(OUT_DIR, 'Posudok_1_FVE_BESS_RE-PLAST.docx');
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(OUT, buf); console.log('Saved:', OUT); });
