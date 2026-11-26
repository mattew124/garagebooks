"""Words for a fill-up's figures, shared by the form's live preview and the expense list.

The figures themselves come from the garagebooks.fuel_fill view (see schema.sql): the distance since the last
fuel-up at once, whatever kind of fill it was, and L/100 km of the tank once it is known.
"""


def describe(row):
    """What a fill-up's row of garagebooks.fuel_fill says, as short sentences for the form. `row` may carry
    litres of None (not typed yet): the distance is still shown, and L/100 km is asked for."""
    lines = []
    if row.get("odometer_km") is None:
        return ["Add the odometer to see the distance since the last fuel-up."]
    distance = row.get("distance_km")
    if distance is None:
        lines.append("This is the first fuel-up with an odometer reading, so there is no distance to show yet.")
    else:
        lines.append(f"{float(distance):,.0f} km since the last fuel-up.")
    if row.get("litres") is None:
        lines.append("Enter the litres to see L/100 km.")
    elif not row.get("full_tank", True):
        lines.append("Partial fill: L/100 km is worked out when the tank is next filled.")
    elif row.get("missed_fill"):
        lines.append("A fuel-up was missed before this one, so there is no L/100 km for this tank.")
    elif row.get("l_per_100km") is not None:
        lines.append(f"{float(row['l_per_100km']):.1f} L/100 km over this tank "
                     f"({float(row['tank_litres']):,.1f} L in {float(row['tank_km']):,.0f} km).")
    else:
        lines.append("No earlier full tank to measure from yet: L/100 km shows from the next fill that fills the tank.")
    return lines
