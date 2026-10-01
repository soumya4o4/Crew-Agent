"""Tests run against the same kind of place registry the app builds from the `airports` table at start-up."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.core import places  # noqa: E402

AIRPORTS = [
    ("IDR", "Indore", "Devi Ahilya Bai Holkar Airport", "India", 22.7218, 75.8011),
    ("BOM", "Mumbai", "Chhatrapati Shivaji Maharaj International Airport", "India", 19.0887, 72.8679),
    ("DEL", "Delhi", "Indira Gandhi International Airport", "India", 28.5562, 77.1),
    ("BLR", "Bengaluru", "Kempegowda International Airport", "India", 13.1989, 77.7068),
    ("HYD", "Hyderabad", "Rajiv Gandhi International Airport", "India", 17.2403, 78.4294),
    ("MAA", "Chennai", "Chennai International Airport", "India", 12.9941, 80.1709),
    ("CCU", "Kolkata", "Netaji Subhas Chandra Bose International Airport", "India", 22.6547, 88.4467),
    ("PNQ", "Pune", "Pune Airport", "India", 18.5822, 73.9197),
    ("GOI", "Goa", "Manohar International Airport", "India", 15.7441, 73.8644),
    ("AMD", "Ahmedabad", "Sardar Vallabhbhai Patel International Airport", "India", 23.0772, 72.6347),
    ("DXB", "Dubai", "Dubai International Airport", "United Arab Emirates", 25.2532, 55.3657),
]
places.load_airports([dict(zip(("code", "city", "name", "country", "lat", "lon"), a)) for a in AIRPORTS])
