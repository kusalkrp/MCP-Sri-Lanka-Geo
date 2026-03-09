# Example Questions — MCP Sri Lanka Geo

> Ask these to an AI agent connected to the `srilanka-geo` MCP server.
> The agent will select the right tool and parameters automatically.

---

## 1. `find_nearby` — Spatial POI search by radius

Finds any POI type within a radius. Best for: "what's around me / around X location".
Filters by `category` and/or `subcategory` exactly.

```
Find all ATMs within 1km of Kandy city centre
Show me pharmacies near Galle Fort
What petrol stations are within 5km of Negombo?
List all banks within 2km of Colombo Fort
Find mosques near Beruwala within 3km
What police stations are in a 10km radius of Anuradhapura?
Show me all schools within 4km of Jaffna city
Find Buddhist temples near Dambulla within 8km
What supermarkets are near the Bandaranaike Airport?
List all ATMs and banks within 3km of Nuwara Eliya
```

---

## 2. `get_poi_details` — Full record for a single POI

Use when you have a POI ID from a previous search and want its full details — address, tags, Wikidata enrichment, quality score, data sources.

```
Show me full details for POI w513773173
What are the OSM tags for n6167545071?
Get all available information about POI r12345
What is the Wikidata entry for POI w341980667?
Show the address and coordinates of POI n5592219321
What is the quality score for w513773173?
Is POI n8307765317 enriched with GeoNames data?
What data sources does POI w67890 come from?
```

---

## 3. `get_administrative_area` — Reverse geocode coordinates

Converts a lat/lng to district and province. Useful when you have GPS coordinates and need to know where in Sri Lanka they are.

```
What district is 6.9344, 79.8428 in?
Which province does the coordinate 7.2906, 80.6337 belong to?
What administrative area is Mirissa beach in? (8.9553, 80.4549)
Tell me the district for coordinates 9.6615, 80.0255
Which province contains the point 6.0535, 80.2210?
What district is Sigiriya Rock in? (7.9572, 80.7603)
Is coordinate 8.3500, 80.5000 in Polonnaruwa or Matale district?
What province is the Sinharaja Forest Reserve in? (6.3853, 80.4667)
```

---

## 4. `validate_coordinates` — Check if coordinates are within Sri Lanka

Use before running spatial queries when you're unsure if a coordinate is valid for Sri Lanka.

```
Are coordinates 6.9344, 79.8428 within Sri Lanka?
Is 12.0, 80.0 a valid Sri Lanka coordinate?
Check if 0, 0 is a valid location in Sri Lanka
Validate coordinates 5.5, 79.8 — are they inside Sri Lanka?
Is 9.9, 81.9 at the boundary of Sri Lanka?
Are these GPS coordinates valid for a Sri Lanka search: 8.3, 85.0?
Check whether 7.5, 81.0 falls inside Sri Lanka
```

---

## 5. `get_coverage_stats` — POI counts by category and district

Shows what types of POIs exist in a district or nationally, and how many. Good for understanding data density before searching.

```
How many POIs are in the Colombo district?
What are the most common POI types across Sri Lanka?
How many hospitals are recorded in Jaffna district?
What categories of places exist in Nuwara Eliya?
Which POI types are most common in Galle?
How many shops are in Kandy district?
What is the total number of POIs in the Vavuniya district?
Compare the number of amenities in Colombo vs Gampaha district
How many tourism POIs exist in Sri Lanka nationally?
What is the breakdown of POI categories in Trincomalee?
```

---

## 6. `search_pois` — Hybrid semantic + spatial search

Best for natural language, fuzzy, or concept-based queries. Combines meaning with location. Use when you're not sure of the exact category/subcategory name.

```
Find tea estates and plantations near Nuwara Eliya
Search for Ayurvedic hospitals near Colombo
Find seafood restaurants near Negombo beach
Look for Buddhist meditation centres in Sri Lanka
Find places related to colonial history near Galle
Search for coworking spaces or business centres in Colombo
Find luxury resorts near Mirissa
Look for traditional Sinhala architecture near Kandy
Find government offices dealing with land registration in Colombo
Search for surf schools or surf spots near Arugam Bay
Find IT companies and tech offices in Colombo 3
Search for gem and jewellery shops in Ratnapura
```

---

## 7. `list_categories` — All category/subcategory combinations with counts

Shows the complete vocabulary of POI types available. Use this to discover what subcategory names to use in other tools.

```
What types of places are recorded in Sri Lanka?
List all POI categories available in Kandy district
What subcategories exist under the amenity category?
Show me all tourism-related subcategories in Sri Lanka
What shop types are recorded in Colombo?
What kinds of offices are mapped in Gampaha district?
List all healthcare-related POI subcategories
What leisure categories exist in Sri Lanka?
Show me all subcategories available in Jaffna
What historic site types are in the dataset?
```

---

## 8. `get_business_density` — Business breakdown by category for a radius

Tells you the mix of POI types at a location — useful for market analysis, site selection, or understanding a neighbourhood.

```
What is the business density around Colombo Fort within 2km?
How many restaurants and cafes are near Kandy Lake?
Analyse the commercial density of Pettah market area
What types of businesses dominate the Bambalapitiya area?
Compare business density in Galle Fort vs Galle town
What is the POI breakdown within 1km of Negombo beach?
How many banks and ATMs are in the Colombo CBD area?
What is the commercial mix in the Wellawatte neighbourhood?
Analyse the density of shops and offices near Maradana station
How busy is the area around Nugegoda junction in terms of POIs?
```

---

## 9. `route_between` — Straight-line distance and bearing between two POIs

Calculates the as-the-crow-flies distance and compass bearing between any two POIs by their ID. Use IDs obtained from other tool results.

```
What is the distance between Nawaloka Hospital and Lady Ridgeway Hospital?
How far is the University of Colombo from the National Museum?
What is the straight-line distance between Galle Fort and Mirissa beach?
Calculate the distance between Kandy Temple of the Tooth and Pinnawala Elephant Orphanage
How far apart are the two Nawaloka Hospital buildings?
What is the bearing from Bandaranaike Airport to Colombo Fort?
What direction is Jaffna from Colombo?
How far is the Colombo Port from the nearest hospital?
```

> **Note:** Requires POI IDs (e.g. `w513773173`). First use `find_nearby` or `search_pois` to get IDs, then call `route_between`.

---

## 10. `find_universities` — Universities and colleges near a point

Specifically targets `amenity=university`, `amenity=college`, and `office=educational_institution` tags.

```
What universities are near Colombo?
Find colleges within 15km of Kandy
Are there any universities near Jaffna?
What higher education institutions are in the Gampaha district area?
Find all colleges within 30km of Kurunegala
What universities are near Moratuwa?
Are there engineering or technology universities near Colombo?
List educational institutions within 20km of Ratnapura
Find universities near the A1 highway corridor
What colleges are accessible from Matara?
```

---

## 11. `find_agricultural_zones` — Farmland, orchards, reservoirs near a point

Finds landuse zones: farmland, orchards, greenhouses, aquaculture areas, vineyards, and irrigation reservoirs.

```
What agricultural zones are near Anuradhapura?
Find farmland areas within 20km of Polonnaruwa
Are there any orchards or plantations near Badulla?
What agricultural land is near the Mahaweli River basin?
Find reservoirs and irrigation tanks near Trincomalee
What farming zones exist near Ampara?
Find agricultural areas near the Dry Zone of Sri Lanka
Are there aquaculture zones near Negombo lagoon?
What landuse types dominate the area around Kurunegala?
Find rice paddy fields near Hambantota
```

---

## 12. `find_businesses_near` — Commercial businesses near a point

Covers shops, offices, restaurants, banks, pharmacies, and all commercial activity. Use `business_type` to narrow down.

```
Find all restaurants within 3km of Colombo Fort
What banks are near Kandy city centre?
Show me all pharmacies within 2km of Dehiwala
Find fuel stations within 10km of Ratnapura
What supermarkets are near Nugegoda?
Find cafes and coffee shops near the Colombo 7 area
What ATMs are near Galle bus station?
Find all offices within 5km of the Colombo Port
Show me fast food restaurants near Bambalapitiya
What shops are near the Pettah market area?
Find all fuel stations on the Colombo-Kandy highway corridor
What businesses are near the Katunayake Free Trade Zone?
```

---

## Multi-tool Workflow Questions

These require the agent to chain multiple tools together:

```
Find the nearest hospital to Kandy Temple of the Tooth and tell me its full details
Search for Ayurvedic centres near Colombo, then get details for the top result
What district is Sigiriya in, and how many tourism POIs does that district have?
Find the two closest universities in Colombo and calculate the distance between them
Search for seafood restaurants near Negombo and show full details for the closest one
Find all hospitals within 5km of Colombo Fort, then calculate the distance between the two closest ones
What are the agricultural zones near Anuradhapura, and what district are they in?
Find banks near Jaffna, validate that the coordinates are correct, then show business density for the area
Search for Buddhist temples near Dambulla, get details for the top result, and show what other POIs are nearby
List all categories in Galle district, then find businesses of the most common category near Galle Fort
```
