/**
 * Matching a data column to a country on the world map.
 *
 * The counterpart to `us-states.ts`, and it carries the same bias: a value that
 * matches nothing must be COUNTED, not dropped. An unshaded country reads as
 * "zero" to anyone looking at the map, so `unmatchedCountries()` exists to let
 * the panel say "we could not place these" out loud.
 *
 * Three things make this harder than the US table:
 *
 *  1. The boundary file (world-atlas, Natural Earth) identifies countries by
 *     ISO 3166-1 NUMERIC id only — no alpha codes, and abbreviated names
 *     ("Bosnia and Herz."). Warehouse columns hold alpha-2, alpha-3 or a full
 *     name. The table below bridges all four.
 *  2. Our data is not always country-grained. The logistics tables identify a
 *     shipment by port ("Jebel Ali (AEJEA)"), and a UN/LOCODE's first two
 *     letters ARE the ISO alpha-2, so one rule resolves that entire column.
 *     Bare city names get a small gazetteer of the hubs that actually appear
 *     in freight and fund data.
 *  3. Regions — "Europe", "EMEA", "APAC" — sit alongside countries in the
 *     private credit and PE GEOGRAPHY columns. They are deliberately NOT
 *     matched. Painting one number across forty-four countries would invent
 *     per-country data that does not exist; a footer naming them is honest.
 *
 * ISO numeric codes are NOT accepted from the data, though `us-states.ts`
 * accepts FIPS. A bare "76" is far likelier to be a quantity than Brazil, and
 * shading a country off a stray number is the kind of wrong nobody catches.
 */

export type Country = {
  /** ISO 3166-1 numeric — the id the boundary file uses. */
  id: string
  alpha2: string
  alpha3: string
  /** Display name. Deliberately fuller than the atlas's own abbreviations. */
  name: string
}

/* Every country in world-atlas countries-50m, by numeric id. Kept in step with
   the atlas by geo-countries.test.ts, which fails if either side gains or
   loses an entry — a country present in the geometry but missing here would be
   permanently unmatchable, and silently so. */
const TABLE: Array<[string, string, string, string]> = [
  ["004", "AF", "AFG", "Afghanistan"],
  ["008", "AL", "ALB", "Albania"],
  ["010", "AQ", "ATA", "Antarctica"],
  ["012", "DZ", "DZA", "Algeria"],
  ["016", "AS", "ASM", "American Samoa"],
  ["020", "AD", "AND", "Andorra"],
  ["024", "AO", "AGO", "Angola"],
  ["028", "AG", "ATG", "Antigua and Barbuda"],
  ["031", "AZ", "AZE", "Azerbaijan"],
  ["032", "AR", "ARG", "Argentina"],
  ["036", "AU", "AUS", "Australia"],
  ["040", "AT", "AUT", "Austria"],
  ["044", "BS", "BHS", "Bahamas"],
  ["048", "BH", "BHR", "Bahrain"],
  ["050", "BD", "BGD", "Bangladesh"],
  ["051", "AM", "ARM", "Armenia"],
  ["052", "BB", "BRB", "Barbados"],
  ["056", "BE", "BEL", "Belgium"],
  ["060", "BM", "BMU", "Bermuda"],
  ["064", "BT", "BTN", "Bhutan"],
  ["068", "BO", "BOL", "Bolivia"],
  ["070", "BA", "BIH", "Bosnia and Herzegovina"],
  ["072", "BW", "BWA", "Botswana"],
  ["076", "BR", "BRA", "Brazil"],
  ["084", "BZ", "BLZ", "Belize"],
  ["086", "IO", "IOT", "British Indian Ocean Territory"],
  ["090", "SB", "SLB", "Solomon Islands"],
  ["092", "VG", "VGB", "British Virgin Islands"],
  ["096", "BN", "BRN", "Brunei"],
  ["100", "BG", "BGR", "Bulgaria"],
  ["104", "MM", "MMR", "Myanmar"],
  ["108", "BI", "BDI", "Burundi"],
  ["112", "BY", "BLR", "Belarus"],
  ["116", "KH", "KHM", "Cambodia"],
  ["120", "CM", "CMR", "Cameroon"],
  ["124", "CA", "CAN", "Canada"],
  ["132", "CV", "CPV", "Cabo Verde"],
  ["136", "KY", "CYM", "Cayman Islands"],
  ["140", "CF", "CAF", "Central African Republic"],
  ["144", "LK", "LKA", "Sri Lanka"],
  ["148", "TD", "TCD", "Chad"],
  ["152", "CL", "CHL", "Chile"],
  ["156", "CN", "CHN", "China"],
  ["158", "TW", "TWN", "Taiwan"],
  ["170", "CO", "COL", "Colombia"],
  ["174", "KM", "COM", "Comoros"],
  ["178", "CG", "COG", "Republic of the Congo"],
  ["180", "CD", "COD", "DR Congo"],
  ["184", "CK", "COK", "Cook Islands"],
  ["188", "CR", "CRI", "Costa Rica"],
  ["191", "HR", "HRV", "Croatia"],
  ["192", "CU", "CUB", "Cuba"],
  ["196", "CY", "CYP", "Cyprus"],
  ["203", "CZ", "CZE", "Czechia"],
  ["204", "BJ", "BEN", "Benin"],
  ["208", "DK", "DNK", "Denmark"],
  ["212", "DM", "DMA", "Dominica"],
  ["214", "DO", "DOM", "Dominican Republic"],
  ["218", "EC", "ECU", "Ecuador"],
  ["222", "SV", "SLV", "El Salvador"],
  ["226", "GQ", "GNQ", "Equatorial Guinea"],
  ["231", "ET", "ETH", "Ethiopia"],
  ["232", "ER", "ERI", "Eritrea"],
  ["233", "EE", "EST", "Estonia"],
  ["234", "FO", "FRO", "Faroe Islands"],
  ["238", "FK", "FLK", "Falkland Islands"],
  ["239", "GS", "SGS", "South Georgia and the South Sandwich Islands"],
  ["242", "FJ", "FJI", "Fiji"],
  ["246", "FI", "FIN", "Finland"],
  ["248", "AX", "ALA", "Aland Islands"],
  ["250", "FR", "FRA", "France"],
  ["258", "PF", "PYF", "French Polynesia"],
  ["260", "TF", "ATF", "French Southern Territories"],
  ["262", "DJ", "DJI", "Djibouti"],
  ["266", "GA", "GAB", "Gabon"],
  ["268", "GE", "GEO", "Georgia"],
  ["270", "GM", "GMB", "Gambia"],
  ["275", "PS", "PSE", "Palestine"],
  ["276", "DE", "DEU", "Germany"],
  ["288", "GH", "GHA", "Ghana"],
  ["296", "KI", "KIR", "Kiribati"],
  ["300", "GR", "GRC", "Greece"],
  ["304", "GL", "GRL", "Greenland"],
  ["308", "GD", "GRD", "Grenada"],
  ["316", "GU", "GUM", "Guam"],
  ["320", "GT", "GTM", "Guatemala"],
  ["324", "GN", "GIN", "Guinea"],
  ["328", "GY", "GUY", "Guyana"],
  ["332", "HT", "HTI", "Haiti"],
  ["334", "HM", "HMD", "Heard Island and McDonald Islands"],
  ["336", "VA", "VAT", "Vatican City"],
  ["340", "HN", "HND", "Honduras"],
  ["344", "HK", "HKG", "Hong Kong"],
  ["348", "HU", "HUN", "Hungary"],
  ["352", "IS", "ISL", "Iceland"],
  ["356", "IN", "IND", "India"],
  ["360", "ID", "IDN", "Indonesia"],
  ["364", "IR", "IRN", "Iran"],
  ["368", "IQ", "IRQ", "Iraq"],
  ["372", "IE", "IRL", "Ireland"],
  ["376", "IL", "ISR", "Israel"],
  ["380", "IT", "ITA", "Italy"],
  ["384", "CI", "CIV", "Cote d'Ivoire"],
  ["388", "JM", "JAM", "Jamaica"],
  ["392", "JP", "JPN", "Japan"],
  ["398", "KZ", "KAZ", "Kazakhstan"],
  ["400", "JO", "JOR", "Jordan"],
  ["404", "KE", "KEN", "Kenya"],
  ["408", "KP", "PRK", "North Korea"],
  ["410", "KR", "KOR", "South Korea"],
  ["414", "KW", "KWT", "Kuwait"],
  ["417", "KG", "KGZ", "Kyrgyzstan"],
  ["418", "LA", "LAO", "Laos"],
  ["422", "LB", "LBN", "Lebanon"],
  ["426", "LS", "LSO", "Lesotho"],
  ["428", "LV", "LVA", "Latvia"],
  ["430", "LR", "LBR", "Liberia"],
  ["434", "LY", "LBY", "Libya"],
  ["438", "LI", "LIE", "Liechtenstein"],
  ["440", "LT", "LTU", "Lithuania"],
  ["442", "LU", "LUX", "Luxembourg"],
  ["446", "MO", "MAC", "Macao"],
  ["450", "MG", "MDG", "Madagascar"],
  ["454", "MW", "MWI", "Malawi"],
  ["458", "MY", "MYS", "Malaysia"],
  ["462", "MV", "MDV", "Maldives"],
  ["466", "ML", "MLI", "Mali"],
  ["470", "MT", "MLT", "Malta"],
  ["478", "MR", "MRT", "Mauritania"],
  ["480", "MU", "MUS", "Mauritius"],
  ["484", "MX", "MEX", "Mexico"],
  ["492", "MC", "MCO", "Monaco"],
  ["496", "MN", "MNG", "Mongolia"],
  ["498", "MD", "MDA", "Moldova"],
  ["499", "ME", "MNE", "Montenegro"],
  ["500", "MS", "MSR", "Montserrat"],
  ["504", "MA", "MAR", "Morocco"],
  ["508", "MZ", "MOZ", "Mozambique"],
  ["512", "OM", "OMN", "Oman"],
  ["516", "NA", "NAM", "Namibia"],
  ["520", "NR", "NRU", "Nauru"],
  ["524", "NP", "NPL", "Nepal"],
  ["528", "NL", "NLD", "Netherlands"],
  ["531", "CW", "CUW", "Curacao"],
  ["533", "AW", "ABW", "Aruba"],
  ["534", "SX", "SXM", "Sint Maarten"],
  ["540", "NC", "NCL", "New Caledonia"],
  ["548", "VU", "VUT", "Vanuatu"],
  ["554", "NZ", "NZL", "New Zealand"],
  ["558", "NI", "NIC", "Nicaragua"],
  ["562", "NE", "NER", "Niger"],
  ["566", "NG", "NGA", "Nigeria"],
  ["570", "NU", "NIU", "Niue"],
  ["574", "NF", "NFK", "Norfolk Island"],
  ["578", "NO", "NOR", "Norway"],
  ["580", "MP", "MNP", "Northern Mariana Islands"],
  ["583", "FM", "FSM", "Micronesia"],
  ["584", "MH", "MHL", "Marshall Islands"],
  ["585", "PW", "PLW", "Palau"],
  ["586", "PK", "PAK", "Pakistan"],
  ["591", "PA", "PAN", "Panama"],
  ["598", "PG", "PNG", "Papua New Guinea"],
  ["600", "PY", "PRY", "Paraguay"],
  ["604", "PE", "PER", "Peru"],
  ["608", "PH", "PHL", "Philippines"],
  ["612", "PN", "PCN", "Pitcairn Islands"],
  ["616", "PL", "POL", "Poland"],
  ["620", "PT", "PRT", "Portugal"],
  ["624", "GW", "GNB", "Guinea-Bissau"],
  ["626", "TL", "TLS", "Timor-Leste"],
  ["630", "PR", "PRI", "Puerto Rico"],
  ["634", "QA", "QAT", "Qatar"],
  ["642", "RO", "ROU", "Romania"],
  ["643", "RU", "RUS", "Russia"],
  ["646", "RW", "RWA", "Rwanda"],
  ["652", "BL", "BLM", "Saint Barthelemy"],
  ["654", "SH", "SHN", "Saint Helena"],
  ["659", "KN", "KNA", "Saint Kitts and Nevis"],
  ["660", "AI", "AIA", "Anguilla"],
  ["662", "LC", "LCA", "Saint Lucia"],
  ["663", "MF", "MAF", "Saint Martin"],
  ["666", "PM", "SPM", "Saint Pierre and Miquelon"],
  ["670", "VC", "VCT", "Saint Vincent and the Grenadines"],
  ["674", "SM", "SMR", "San Marino"],
  ["678", "ST", "STP", "Sao Tome and Principe"],
  ["682", "SA", "SAU", "Saudi Arabia"],
  ["686", "SN", "SEN", "Senegal"],
  ["688", "RS", "SRB", "Serbia"],
  ["690", "SC", "SYC", "Seychelles"],
  ["694", "SL", "SLE", "Sierra Leone"],
  ["702", "SG", "SGP", "Singapore"],
  ["703", "SK", "SVK", "Slovakia"],
  ["704", "VN", "VNM", "Vietnam"],
  ["705", "SI", "SVN", "Slovenia"],
  ["706", "SO", "SOM", "Somalia"],
  ["710", "ZA", "ZAF", "South Africa"],
  ["716", "ZW", "ZWE", "Zimbabwe"],
  ["724", "ES", "ESP", "Spain"],
  ["728", "SS", "SSD", "South Sudan"],
  ["729", "SD", "SDN", "Sudan"],
  ["732", "EH", "ESH", "Western Sahara"],
  ["740", "SR", "SUR", "Suriname"],
  ["748", "SZ", "SWZ", "Eswatini"],
  ["752", "SE", "SWE", "Sweden"],
  ["756", "CH", "CHE", "Switzerland"],
  ["760", "SY", "SYR", "Syria"],
  ["762", "TJ", "TJK", "Tajikistan"],
  ["764", "TH", "THA", "Thailand"],
  ["768", "TG", "TGO", "Togo"],
  ["776", "TO", "TON", "Tonga"],
  ["780", "TT", "TTO", "Trinidad and Tobago"],
  ["784", "AE", "ARE", "United Arab Emirates"],
  ["788", "TN", "TUN", "Tunisia"],
  ["792", "TR", "TUR", "Turkey"],
  ["795", "TM", "TKM", "Turkmenistan"],
  ["796", "TC", "TCA", "Turks and Caicos Islands"],
  ["800", "UG", "UGA", "Uganda"],
  ["804", "UA", "UKR", "Ukraine"],
  ["807", "MK", "MKD", "North Macedonia"],
  ["818", "EG", "EGY", "Egypt"],
  ["826", "GB", "GBR", "United Kingdom"],
  ["831", "GG", "GGY", "Guernsey"],
  ["832", "JE", "JEY", "Jersey"],
  ["833", "IM", "IMN", "Isle of Man"],
  ["834", "TZ", "TZA", "Tanzania"],
  ["840", "US", "USA", "United States"],
  ["850", "VI", "VIR", "U.S. Virgin Islands"],
  ["854", "BF", "BFA", "Burkina Faso"],
  ["858", "UY", "URY", "Uruguay"],
  ["860", "UZ", "UZB", "Uzbekistan"],
  ["862", "VE", "VEN", "Venezuela"],
  ["876", "WF", "WLF", "Wallis and Futuna"],
  ["882", "WS", "WSM", "Samoa"],
  ["887", "YE", "YEM", "Yemen"],
  ["894", "ZM", "ZMB", "Zambia"],
]

export const COUNTRIES: Country[] = TABLE.map(([id, alpha2, alpha3, name]) => ({
  id,
  alpha2,
  alpha3,
  name,
}))

/**
 * Lowercased, unaccented, unpunctuated.
 *
 * Dots and apostrophes are DELETED rather than turned into separators, so
 * "U.A.E." collapses to "uae" and "Cote d'Ivoire" to "cote divoire". Every
 * other separator becomes a single space, which is what makes "Korea, South"
 * and "Korea South" the same key.
 */
function normalize(value: unknown): string {
  return String(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/['\u2019.]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
}

/**
 * The names a column actually uses, beyond the canonical one.
 *
 * Each is a real spelling seen in warehouse data or an obvious thing a person
 * would type — not an attempt at every exonym. "England" resolving to the UK
 * is a deliberate simplification: the boundary file has no England.
 */
const ALIASES: Record<string, string> = {
  usa: "US",
  us: "US",
  "united states of america": "US",
  america: "US",
  uk: "GB",
  "great britain": "GB",
  britain: "GB",
  england: "GB",
  scotland: "GB",
  wales: "GB",
  "northern ireland": "GB",
  uae: "AE",
  emirates: "AE",
  "arab emirates": "AE",
  "the emirates": "AE",
  korea: "KR",
  "republic of korea": "KR",
  "korea south": "KR",
  "s korea": "KR",
  dprk: "KP",
  "korea north": "KP",
  "russian federation": "RU",
  "viet nam": "VN",
  "czech republic": "CZ",
  turkiye: "TR",
  holland: "NL",
  "the netherlands": "NL",
  "ivory coast": "CI",
  swaziland: "SZ",
  burma: "MM",
  "democratic republic of the congo": "CD",
  drc: "CD",
  "congo kinshasa": "CD",
  zaire: "CD",
  congo: "CG",
  "congo brazzaville": "CG",
  "lao pdr": "LA",
  "syrian arab republic": "SY",
  "islamic republic of iran": "IR",
  "united republic of tanzania": "TZ",
  "bolivarian republic of venezuela": "VE",
  "plurinational state of bolivia": "BO",
  "republic of moldova": "MD",
  macedonia: "MK",
  fyrom: "MK",
  "cape verde": "CV",
  "east timor": "TL",
  "holy see": "VA",
  vatican: "VA",
  "brunei darussalam": "BN",
  "hong kong sar": "HK",
  "hong kong china": "HK",
  macau: "MO",
  "chinese taipei": "TW",
  ksa: "SA",
  "kingdom of saudi arabia": "SA",
  "st kitts and nevis": "KN",
  "st lucia": "LC",
  "st vincent and the grenadines": "VC",
  "st barthelemy": "BL",
  "st martin": "MF",
  "st pierre and miquelon": "PM",
  "st helena": "SH",
  trinidad: "TT",
  bosnia: "BA",
}

/**
 * Cities and ports that stand in for their country.
 *
 * The freight tables identify a shipment by port, and a fund's GEOGRAPHY is
 * sometimes a financial centre. Rolling these up is what a country map is for
 * — Dubai and Abu Dhabi both shade the UAE, summed. Only hubs that plausibly
 * appear in freight or fund data are listed; anything else falls through to
 * the unmatched footer rather than being guessed at.
 */
const CITIES: Record<string, string> = {
  // Gulf and Middle East
  dubai: "AE",
  "jebel ali": "AE",
  "abu dhabi": "AE",
  sharjah: "AE",
  "khalifa port": "AE",
  doha: "QA",
  "hamad port": "QA",
  manama: "BH",
  "kuwait city": "KW",
  muscat: "OM",
  salalah: "OM",
  sohar: "OM",
  jeddah: "SA",
  dammam: "SA",
  riyadh: "SA",
  "king abdullah port": "SA",
  "tel aviv": "IL",
  haifa: "IL",
  beirut: "LB",
  amman: "JO",
  aqaba: "JO",
  // South Asia
  mumbai: "IN",
  "nhava sheva": "IN",
  "jawaharlal nehru": "IN",
  chennai: "IN",
  kolkata: "IN",
  "new delhi": "IN",
  delhi: "IN",
  bengaluru: "IN",
  bangalore: "IN",
  hyderabad: "IN",
  mundra: "IN",
  kochi: "IN",
  cochin: "IN",
  vishakhapatnam: "IN",
  karachi: "PK",
  "port qasim": "PK",
  lahore: "PK",
  colombo: "LK",
  chittagong: "BD",
  dhaka: "BD",
  // East and Southeast Asia
  shanghai: "CN",
  shenzhen: "CN",
  ningbo: "CN",
  qingdao: "CN",
  guangzhou: "CN",
  tianjin: "CN",
  xiamen: "CN",
  dalian: "CN",
  beijing: "CN",
  kaohsiung: "TW",
  taipei: "TW",
  busan: "KR",
  seoul: "KR",
  incheon: "KR",
  tokyo: "JP",
  yokohama: "JP",
  kobe: "JP",
  nagoya: "JP",
  osaka: "JP",
  "port klang": "MY",
  "tanjung pelepas": "MY",
  "kuala lumpur": "MY",
  penang: "MY",
  "laem chabang": "TH",
  bangkok: "TH",
  "ho chi minh city": "VN",
  "ho chi minh": "VN",
  saigon: "VN",
  haiphong: "VN",
  "cai mep": "VN",
  hanoi: "VN",
  jakarta: "ID",
  "tanjung priok": "ID",
  surabaya: "ID",
  manila: "PH",
  "subic bay": "PH",
  // Europe
  rotterdam: "NL",
  amsterdam: "NL",
  antwerp: "BE",
  brussels: "BE",
  hamburg: "DE",
  bremerhaven: "DE",
  frankfurt: "DE",
  munich: "DE",
  berlin: "DE",
  felixstowe: "GB",
  southampton: "GB",
  london: "GB",
  liverpool: "GB",
  "le havre": "FR",
  marseille: "FR",
  paris: "FR",
  barcelona: "ES",
  valencia: "ES",
  algeciras: "ES",
  madrid: "ES",
  genoa: "IT",
  "gioia tauro": "IT",
  milan: "IT",
  trieste: "IT",
  piraeus: "GR",
  athens: "GR",
  lisbon: "PT",
  gdansk: "PL",
  warsaw: "PL",
  gothenburg: "SE",
  stockholm: "SE",
  copenhagen: "DK",
  oslo: "NO",
  helsinki: "FI",
  zurich: "CH",
  geneva: "CH",
  vienna: "AT",
  dublin: "IE",
  istanbul: "TR",
  ambarli: "TR",
  "st petersburg": "RU",
  moscow: "RU",
  valletta: "MT",
  // Africa
  durban: "ZA",
  "cape town": "ZA",
  johannesburg: "ZA",
  mombasa: "KE",
  nairobi: "KE",
  "dar es salaam": "TZ",
  lagos: "NG",
  apapa: "NG",
  tangier: "MA",
  "tanger med": "MA",
  casablanca: "MA",
  alexandria: "EG",
  "port said": "EG",
  cairo: "EG",
  abidjan: "CI",
  tema: "GH",
  accra: "GH",
  "port louis": "MU",
  // Americas
  "new york": "US",
  newark: "US",
  "los angeles": "US",
  "long beach": "US",
  savannah: "US",
  houston: "US",
  seattle: "US",
  tacoma: "US",
  oakland: "US",
  miami: "US",
  norfolk: "US",
  charleston: "US",
  chicago: "US",
  "new orleans": "US",
  baltimore: "US",
  boston: "US",
  "san francisco": "US",
  vancouver: "CA",
  montreal: "CA",
  toronto: "CA",
  halifax: "CA",
  "prince rupert": "CA",
  manzanillo: "MX",
  veracruz: "MX",
  "lazaro cardenas": "MX",
  "mexico city": "MX",
  "panama city": "PA",
  balboa: "PA",
  colon: "PA",
  kingston: "JM",
  "grand cayman": "KY",
  santos: "BR",
  "sao paulo": "BR",
  paranagua: "BR",
  "rio de janeiro": "BR",
  "buenos aires": "AR",
  callao: "PE",
  lima: "PE",
  cartagena: "CO",
  bogota: "CO",
  "san antonio": "CL",
  valparaiso: "CL",
  santiago: "CL",
  // Oceania
  sydney: "AU",
  melbourne: "AU",
  brisbane: "AU",
  fremantle: "AU",
  auckland: "NZ",
  tauranga: "NZ",
}

const BY_ID = new Map(COUNTRIES.map((c) => [c.id, c]))
const BY_ALPHA2 = new Map(COUNTRIES.map((c) => [c.alpha2, c]))

/** Every spelling a data value might use, pointing at its country. */
const BY_VALUE = new Map<string, Country>()
for (const country of COUNTRIES) {
  BY_VALUE.set(normalize(country.alpha2), country)
  BY_VALUE.set(normalize(country.alpha3), country)
  BY_VALUE.set(normalize(country.name), country)
}
for (const [alias, alpha2] of Object.entries(ALIASES)) {
  const country = BY_ALPHA2.get(alpha2)
  if (country) BY_VALUE.set(normalize(alias), country)
}
for (const [city, alpha2] of Object.entries(CITIES)) {
  const country = BY_ALPHA2.get(alpha2)
  // A city never overrides a country of the same name. Singapore, Panama and
  // Hong Kong are both, and the country is what a choropleth means.
  if (country && !BY_VALUE.has(normalize(city)))
    BY_VALUE.set(normalize(city), country)
}

/**
 * The country code inside a UN/LOCODE, if the value carries one.
 *
 * Freight rows read "Jebel Ali, Dubai (AEJEA)", and a LOCODE's first two
 * characters ARE the ISO alpha-2, so this one rule resolves a whole column no
 * gazetteer would ever cover.
 *
 * Only the BRACKETED form counts. Reading a bare five-character token as a
 * LOCODE looks harmless and is not: "Alpha" becomes Albania, "Cargo" Canada
 * and "LATAM" Laos, each of them shading a country nobody has data on, with
 * nothing on screen to say it happened. A missed match lands in the footer
 * where someone can see it; a wrong one never does.
 */
function fromLocode(raw: string): Country | undefined {
  const bracketed = raw.match(/\(\s*([A-Z]{2})[A-Z0-9]{3}\s*\)/)
  return bracketed ? BY_ALPHA2.get(bracketed[1]) : undefined
}

/** The country a boundary-file feature belongs to. */
export function countryById(id: unknown): Country | undefined {
  return BY_ID.get(String(id).padStart(3, "0"))
}

/** The country a data value names, by code, name, LOCODE or known city. */
export function matchCountry(value: unknown): Country | undefined {
  if (value === null || value === undefined) return undefined
  const raw = String(value)

  const direct = BY_VALUE.get(normalize(raw))
  if (direct) return direct

  const locode = fromLocode(raw)
  if (locode) return locode

  // "Jebel Ali, Dubai" and "Mumbai, India" — the last comma-separated part is
  // conventionally the broadest, so try it before giving up. Two-letter tails
  // are refused: "Chicago, IL" would otherwise resolve to Israel, and every
  // US "City, ST" column would quietly land somewhere in the Middle East.
  const parts = raw.split(",")
  if (parts.length > 1) {
    const tail = normalize(parts[parts.length - 1])
    if (tail.length > 2) {
      const country = BY_VALUE.get(tail)
      if (country) return country
    }
  }
  return undefined
}

/** Values that matched no country, so the panel can say so rather than hide it. */
export function unmatchedCountries(values: unknown[]): string[] {
  const missed: string[] = []
  for (const value of values) {
    if (value === null || value === undefined || String(value).trim() === "")
      continue
    if (!matchCountry(value)) missed.push(String(value))
  }
  return [...new Set(missed)]
}
