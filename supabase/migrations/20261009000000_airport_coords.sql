-- Airports get coordinates, so "nearest city", airport directions and airport buffers work for any city we add.
alter table public.airports add column if not exists lat double precision;
alter table public.airports add column if not exists lon double precision;

update public.airports a set lat = v.lat, lon = v.lon
from (values
    ('IDR', 22.7218, 75.8011),
    ('BOM', 19.0887, 72.8679),
    ('DEL', 28.5562, 77.1),
    ('BLR', 13.1989, 77.7068),
    ('HYD', 17.2403, 78.4294),
    ('MAA', 12.9941, 80.1709),
    ('CCU', 22.6547, 88.4467),
    ('PNQ', 18.5822, 73.9197),
    ('GOI', 15.7441, 73.8644),
    ('AMD', 23.0772, 72.6347),
    ('DXB', 25.2532, 55.3657)
) as v(code, lat, lon)
where a.code = v.code and a.lat is null;
