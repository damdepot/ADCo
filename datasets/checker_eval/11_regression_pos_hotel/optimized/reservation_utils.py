# ADCO_OPTIMIZED: 11_regression_pos_hotel
"""Reservation formatting helpers — extracted from main.py."""


def format_reservation(booking_id, room_id, guest_name, date):
    return {
        "id": booking_id,
        "room_id": room_id,
        "guest_name": guest_name,
        "date": date,
    }
