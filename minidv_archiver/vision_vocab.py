"""What the "Szukaj" tags are: (id, Polish label, English label, category, prompts).

The prompts are what the image model compares a frame with (English: the model
was trained mostly on English captions). A tag with several prompts — a country
seen through its landmarks — matches a frame when any one of them does. Edit
freely: the indexer re-tags every analysed frame when this list changes (cheap,
no frame is decoded again).
"""
from __future__ import annotations

CATEGORIES = {
    "events": ("Wydarzenia", "Events"),
    "places": ("Miejsca i krajobraz", "Places & landscape"),
    "home": ("W domu", "At home"),
    "things": ("Przedmioty", "Things"),
    "vehicles": ("Pojazdy", "Vehicles"),
    "animals": ("Zwierzęta", "Animals"),
    "countries": ("Kraje i zabytki", "Countries & landmarks"),
}

_T = [
    # events
    ("wesele", "Wesele", "Wedding", "events", ["a wedding", "a bride in a white wedding dress", "a wedding reception"]),
    ("slub-w-kosciele", "Ślub w kościele", "Church wedding", "events", ["a wedding ceremony in a church"]),
    ("urodziny", "Urodziny", "Birthday", "events", ["a birthday party", "a birthday cake with candles"]),
    ("tort", "Tort", "Cake", "events", ["a cake on a table"]),
    ("boze-narodzenie", "Boże Narodzenie", "Christmas", "events", ["a Christmas tree with decorations", "Christmas Eve dinner"]),
    ("wielkanoc", "Wielkanoc", "Easter", "events", ["Easter eggs", "an Easter basket"]),
    ("komunia", "Komunia", "First communion", "events", ["a child in a white first communion robe"]),
    ("chrzest", "Chrzest", "Baptism", "events", ["a baby baptism in a church"]),
    ("impreza", "Impreza", "Party", "events", ["a party with people dancing", "people celebrating at a party"]),
    ("taniec", "Taniec", "Dancing", "events", ["people dancing"]),
    ("koncert", "Koncert / występ", "Concert / show", "events", ["a concert on a stage", "children performing on a stage"]),
    ("szkola", "Szkoła", "School", "events", ["a school classroom", "a school event with children"]),
    ("przedszkole", "Przedszkole", "Kindergarten", "events", ["a kindergarten with small children"]),
    ("obiad", "Przy stole", "At the table", "events", ["people eating dinner at a table", "a family meal"]),
    ("grill", "Grill / ognisko", "Barbecue / bonfire", "events", ["a barbecue grill", "a bonfire"]),
    ("piknik", "Piknik", "Picnic", "events", ["a picnic on the grass"]),
    ("prezenty", "Prezenty", "Presents", "events", ["unwrapping presents", "gift boxes"]),
    ("fajerwerki", "Fajerwerki", "Fireworks", "events", ["fireworks in the night sky"]),
    ("sport", "Sport", "Sport", "events", ["people playing sports"]),
    ("pilka-nozna", "Piłka nożna", "Football", "events", ["playing football", "a soccer ball"]),
    ("kapiel", "Kąpiel / pływanie", "Swimming", "events", ["people swimming", "children bathing in water"]),
    ("basen", "Basen", "Swimming pool", "events", ["a swimming pool"]),
    ("sanki", "Sanki / zjeżdżanie", "Sledging", "events", ["children sledging in the snow"]),
    ("narty", "Narty", "Skiing", "events", ["skiing on a slope"]),
    ("spacer", "Spacer", "A walk", "events", ["people walking outdoors"]),
    ("zakupy", "Zakupy", "Shopping", "events", ["a shop", "a market stall"]),
    ("praca-w-ogrodzie", "Praca w ogrodzie", "Gardening", "events", ["gardening", "working in a vegetable garden"]),
    ("budowa", "Budowa / remont", "Building work", "events", ["a construction site", "renovating a house"]),
    # places & landscape
    ("plaza", "Plaża", "Beach", "places", ["a beach"]),
    ("morze", "Morze", "Sea", "places", ["the sea", "sea waves"]),
    ("jezioro", "Jezioro", "Lake", "places", ["a lake"]),
    ("staw", "Staw", "Pond", "places", ["a pond"]),
    ("rzeka", "Rzeka", "River", "places", ["a river"]),
    ("woda", "Woda", "Water", "places", ["water"]),
    ("wodospad", "Wodospad", "Waterfall", "places", ["a waterfall"]),
    ("gory", "Góry", "Mountains", "places", ["mountains", "a mountain landscape"]),
    ("las", "Las", "Forest", "places", ["a forest"]),
    ("laka", "Łąka / pole", "Meadow / field", "places", ["a meadow", "a farm field"]),
    ("ogrod", "Ogród", "Garden", "places", ["a garden"]),
    ("park", "Park", "Park", "places", ["a city park"]),
    ("plac-zabaw", "Plac zabaw", "Playground", "places", ["a playground"]),
    ("wies", "Wieś", "Countryside", "places", ["a village", "a farm"]),
    ("miasto", "Miasto", "City", "places", ["a city street", "a town square"]),
    ("budynki", "Budynki", "Buildings", "places", ["buildings"]),
    ("blok", "Blok", "Apartment block", "places", ["an apartment block"]),
    ("dom", "Dom", "House", "places", ["a house"]),
    ("kosciol", "Kościół", "Church", "places", ["a church", "the inside of a church"]),
    ("zamek", "Zamek", "Castle", "places", ["a castle"]),
    ("most", "Most", "Bridge", "places", ["a bridge"]),
    ("port", "Port", "Harbour", "places", ["a harbour with boats"]),
    ("lotnisko", "Lotnisko", "Airport", "places", ["an airport"]),
    ("dworzec", "Dworzec", "Station", "places", ["a railway station"]),
    ("stadion", "Stadion", "Stadium", "places", ["a stadium"]),
    ("restauracja", "Restauracja", "Restaurant", "places", ["a restaurant"]),
    ("cmentarz", "Cmentarz", "Cemetery", "places", ["a cemetery"]),
    ("snieg", "Śnieg", "Snow", "places", ["snow", "a snowy landscape"]),
    ("deszcz", "Deszcz", "Rain", "places", ["rain"]),
    ("zachod-slonca", "Zachód słońca", "Sunset", "places", ["a sunset"]),
    ("noc", "Noc", "Night", "places", ["a street at night with lights", "a night sky"]),
    # at home
    ("kuchnia", "Kuchnia", "Kitchen", "home", ["a kitchen"]),
    ("salon", "Pokój / salon", "Living room", "home", ["a living room"]),
    ("sypialnia", "Sypialnia", "Bedroom", "home", ["a bedroom", "a bed"]),
    ("lazienka", "Łazienka", "Bathroom", "home", ["a bathroom"]),
    ("balkon", "Balkon", "Balcony", "home", ["a balcony"]),
    ("gotowanie", "Gotowanie", "Cooking", "home", ["cooking food in a kitchen"]),
    # things
    ("komputer", "Komputer", "Computer", "things", ["a computer", "a desktop computer"]),
    ("laptop", "Laptop", "Laptop", "things", ["a laptop"]),
    ("monitor-crt", "Monitor CRT", "CRT monitor", "things", ["an old CRT computer monitor"]),
    ("telewizor", "Telewizor", "Television", "things", ["a television set", "an old CRT television"]),
    ("telefon", "Telefon komórkowy", "Mobile phone", "things", ["a mobile phone"]),
    ("kable", "Kable", "Cables", "things", ["cables and wires"]),
    ("elektronika", "Elektronika", "Electronics", "things", ["electronic equipment", "a circuit board"]),
    ("kamera", "Kamera / aparat", "Camera", "things", ["a video camera", "a photo camera"]),
    ("gitara", "Gitara", "Guitar", "things", ["a guitar"]),
    ("pianino", "Pianino / keyboard", "Piano / keyboard", "things", ["a piano", "a musical keyboard"]),
    ("zabawki", "Zabawki", "Toys", "things", ["toys", "children's toys on the floor"]),
    ("klocki", "Klocki", "Building blocks", "things", ["lego bricks", "building blocks"]),
    ("ksiazki", "Książki", "Books", "things", ["books"]),
    ("kwiaty", "Kwiaty", "Flowers", "things", ["flowers", "a bouquet of flowers"]),
    ("balony", "Balony", "Balloons", "things", ["balloons"]),
    ("swieczki", "Świeczki", "Candles", "things", ["candles"]),
    ("jedzenie", "Jedzenie", "Food", "things", ["food on a plate"]),
    ("pilka", "Piłka", "Ball", "things", ["a ball"]),
    ("hustawka", "Huśtawka", "Swing", "things", ["a swing"]),
    ("namiot", "Namiot", "Tent", "things", ["a tent"]),
    # vehicles
    ("samochod", "Samochód", "Car", "vehicles", ["a car"]),
    ("autobus", "Autobus", "Bus", "vehicles", ["a bus"]),
    ("pociag", "Pociąg", "Train", "vehicles", ["a train"]),
    ("samolot", "Samolot", "Aeroplane", "vehicles", ["an aeroplane"]),
    ("lodz", "Łódź / statek", "Boat / ship", "vehicles", ["a boat", "a ship"]),
    ("rower", "Rower", "Bicycle", "vehicles", ["a bicycle"]),
    ("motocykl", "Motocykl", "Motorbike", "vehicles", ["a motorcycle"]),
    ("traktor", "Traktor", "Tractor", "vehicles", ["a tractor"]),
    ("wozek", "Wózek dziecięcy", "Pram", "vehicles", ["a baby stroller"]),
    # animals
    ("pies", "Pies", "Dog", "animals", ["a dog"]),
    ("kot", "Kot", "Cat", "animals", ["a cat"]),
    ("kon", "Koń", "Horse", "animals", ["a horse"]),
    ("krowa", "Krowa", "Cow", "animals", ["a cow"]),
    ("kury", "Kury / drób", "Chickens", "animals", ["chickens", "ducks or geese"]),
    ("ptaki", "Ptaki", "Birds", "animals", ["birds"]),
    ("ryby", "Ryby / wędkowanie", "Fish / fishing", "animals", ["a person fishing with a fishing rod", "fish in an aquarium"]),
    # countries & landmarks (no GPS on tapes: seen through what's characteristic)
    ("wlochy", "Włochy", "Italy", "countries", ["the Colosseum in Rome", "the Leaning Tower of Pisa",
                                                 "Venice canals with gondolas", "St. Peter's Square in the Vatican",
                                                 "the Duomo cathedral in Milan", "an Italian old town"]),
    ("grecja", "Grecja", "Greece", "countries", ["the Acropolis in Athens", "an ancient Greek temple with columns",
                                                 "white houses with blue domes in Santorini", "a Greek island"]),
    ("chorwacja", "Chorwacja", "Croatia", "countries", ["the old town of Dubrovnik", "the Croatian Adriatic coast",
                                                        "a Dalmatian stone town by the sea"]),
    ("hiszpania", "Hiszpania", "Spain", "countries", ["the Sagrada Familia in Barcelona", "a Spanish beach resort"]),
    ("francja", "Francja", "France", "countries", ["the Eiffel Tower in Paris", "Paris streets"]),
    ("egipt", "Egipt", "Egypt", "countries", ["the pyramids of Giza", "the Egyptian desert"]),
    ("turcja", "Turcja", "Turkey", "countries", ["the Hagia Sophia in Istanbul", "a Turkish bazaar"]),
    ("czechy", "Czechy", "Czechia", "countries", ["Charles Bridge in Prague", "Prague old town"]),
    ("gory-tatry", "Tatry", "Tatra mountains", "countries", ["the Tatra mountains", "Giewont mountain in Zakopane"]),
    ("baltyk", "Bałtyk", "Baltic coast", "countries", ["a Baltic sea beach with dunes", "a pier on the Baltic sea"]),
    ("krakow", "Kraków", "Kraków", "countries", ["Wawel castle in Kraków", "the main market square in Kraków"]),
    ("warszawa", "Warszawa", "Warsaw", "countries", ["the Palace of Culture and Science in Warsaw",
                                                     "the old town of Warsaw"]),
]

TAGS = [{"id": i, "pl": pl, "en": en, "cat": cat, "prompts": prompts} for i, pl, en, cat, prompts in _T]
BY_ID = {t["id"]: t for t in TAGS}

# Stage 2 ("destylat", VisionService.distill): a frame keeps a tag only if the tag
# beats what it's mistaken for. GENERIC = pictures that resemble many tags without
# containing them; RIVALS = what one tag in particular gets confused with (seen on
# the archive: sunlit sand and white walls as snow, a blurry lawn as rain...).
GENERIC = ["a blurry photo", "an overexposed white photo", "a very dark photo", "lens flare", "a photo of a wall"]
# generic descriptions a tag is allowed to look like (night pictures *are* dark)
GENERIC_OK = {"noc": {"a very dark photo"}, "fajerwerki": {"a very dark photo"}}
RIVALS = {
    "snieg": ["sand on a beach", "white sand", "a white wall", "the sea", "a grey sky", "white bed sheets"],
    "plaza": ["snow"],
    "morze": ["a lake", "the sky"],
    "deszcz": ["a dirty window", "film grain noise"],
    "noc": ["a dark room"],
    "woda": ["the sky", "a window"],
    "las": ["a park"],
    "laka": ["a lawn in a garden"],
}
