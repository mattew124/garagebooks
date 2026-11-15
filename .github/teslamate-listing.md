<!-- readme-features: bc90e7401571 -->
## [GarageBooks](https://github.com/mattew124/garagebooks)

Expense tracking and total cost of ownership for your car. A small self-hosted web app records costs such as insurance, registration, servicing and tyres, and keeps their invoices in OneDrive or in a folder on your own server. A Grafana dashboard puts those costs next to the drive and charge data TeslaMate records. It is not MQTT-based: it reads TeslaMate's `cars`, `positions` and `charging_processes` tables directly, keeps its own data in a separate `expenses` schema, never writes to TeslaMate's tables, and refuses to start on a TeslaMate release newer than the one it was tested with.

This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project.

LINK: [github.com/mattew124/garagebooks](https://github.com/mattew124/garagebooks)
