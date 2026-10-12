"""Laboratorio 4: CLI de administracion de acceso con Floodlight.

Ejecutar: python3 main.py [--database database.yaml] [--controller 127.0.0.1]
Dependencias: pip install PyYAML requests aiohttp
Los cambios de alumnos/cursos se guardan con Exportar. El registro de conexiones
se guarda junto al YAML; cargarlo o salir nunca instala ni elimina flows.
"""

import argparse
import ipaddress
from pathlib import Path
import re
import yaml
import json
import requests
import asyncio
import aiohttp


class Student:
    def __init__(self, name, mac, code=None):
        self.name = name
        self.mac = mac
        self.code = code


class Service:
    def __init__(self, name, protocol, port):
        self.name = name
        self.protocol = protocol
        self.port = port


class Server:
    def __init__(self, name, ip, services):
        self.name = name
        self.ip = ip
        self.services = [
            Service(s["name"], s["protocol"], s["port"])
            for s in services
        ]


class Course:
    def __init__(self, name, state, students, servers, code=None):
        self.name = name
        self.code = code
        self.state = state
        self.students = students
        self.servers = servers

    def add_student(self, student_name):
        if student_name not in self.students:
            self.students.append(student_name)

    def remove_student(self, student_name):
        if student_name in self.students:
            self.students.remove(student_name)


def load_data(filename):
    """Cargar el esquema original; conservar Student(name, mac) y el retorno."""
    with open(filename, "r", encoding="utf-8-sig") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError("El YAML debe contener students, servers y courses.")
    for key in ("students", "servers", "courses"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"{key} debe ser una lista.")
    students = [Student(x["name"], x["mac"], x.get("code")) for x in data["students"]]
    servers = [Server(x["name"], x["ip"], x["services"]) for x in data["servers"]]
    courses = [Course(x["name"], x["state"], x["students"], x["servers"],
                      x.get("code")) for x in data["courses"]]
    for objects, label in ((students, "alumnos"), (servers, "servidores"),
                           (courses, "cursos")):
        names = [x.name for x in objects]
        if any(not isinstance(n, str) or not n.strip() for n in names):
            raise ValueError(f"Nombres vacios o invalidos en {label}.")
        if len(names) != len(set(names)):
            raise ValueError(f"Nombres duplicados en {label}.")
    student_names = {x.name for x in students}
    server_map = {x.name: x for x in servers}
    for student in students:
        if not isinstance(student.mac, str):
            raise ValueError(f"MAC invalida: {student.name}.")
    for server in servers:
        ipaddress.IPv4Address(server.ip)
        service_names = [s.name for s in server.services]
        if len(service_names) != len(set(service_names)):
            raise ValueError(f"Servicios duplicados: {server.name}.")
        for service in server.services:
            transport(service.protocol, service.port)
    for course in courses:
        if course.state not in ("ACTIVE", "INACTIVE"):
            raise ValueError(f"Estado invalido en {course.name}.")
        if not isinstance(course.students, list) or not isinstance(course.servers, list):
            raise ValueError(f"Alumnos y servidores deben ser listas en {course.name}.")
        if not set(course.students) <= student_names:
            raise ValueError(f"Alumno desconocido en {course.name}.")
        for permission in course.servers:
            server = server_map.get(permission["name"])
            if server is None or not isinstance(permission["allowed_services"], list):
                raise ValueError(f"Servidor o permisos invalidos en {course.name}.")
            if not set(permission["allowed_services"]) <= {s.name for s in server.services}:
                raise ValueError(f"Servicio desconocido en {course.name}.")
    return students, servers, courses


def controller_get(controller_ip, endpoint, params=None):
    response = requests.get(f"http://{controller_ip}:8080{endpoint}",
                            params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def get_attachment_point(controller_ip, mac):
    devices = controller_get(controller_ip, "/wm/device/", {"mac": mac})
    if not isinstance(devices, list) or len(devices) != 1:
        raise ValueError(f"No hay un unico host para {mac}. Revise el descubrimiento.")
    points = devices[0].get("attachmentPoint", [])
    if not points:
        return None, None
    if len(points) != 1:
        raise ValueError(f"Multiples puntos de conexion para {mac}.")
    return points[0]["switchDPID"], port_number(points[0]["port"])


def port_number(port):
    return int(port["portNumber"] if isinstance(port, dict) else port)


def validate_route(route):
    if not isinstance(route, list) or not route or len(route) % 2:
        raise ValueError("Ruta vacia o incompleta; no se instalaron flows.")
    for i in range(0, len(route), 2):
        if route[i]["switch"] != route[i + 1]["switch"]:
            raise ValueError("Los puertos de entrada y salida deben pertenecer al mismo switch.")
        port_number(route[i]["port"])
        port_number(route[i + 1]["port"])


def get_route(controller_ip, src_mac, dst_mac):
    src_sw, src_port = get_attachment_point(controller_ip, src_mac)
    dst_sw, dst_port = get_attachment_point(controller_ip, dst_mac)
    if src_sw is None or dst_sw is None:
        raise ValueError("El controlador aun no conoce la ubicacion de ambos hosts.")
    route = controller_get(controller_ip,
        f"/wm/topology/route/{src_sw}/{src_port}/{dst_sw}/{dst_port}/json")
    validate_route(route)
    return route


def transport(protocol, port):
    protocol = str(protocol).strip().upper()
    if protocol not in ("TCP", "UDP"):
        raise ValueError("Solo se admiten TCP y UDP.")
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("El puerto debe ser un entero entre 1 y 65535.")
    return protocol.lower(), "0x06" if protocol == "TCP" else "0x11"


def valid_mac(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
        raise ValueError(f"MAC invalida: {value}. Debe tener seis grupos, por ejemplo fa:16:3e:e1:2f:77.")
    return value.lower()


def valid_handler(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("El handler debe contener 1-80 letras, numeros, guiones o guiones bajos.")
    return value


async def push_flow(session, controller_ip, flow):
    url = f"http://{controller_ip}:8080/wm/staticflowpusher/json"

    async with session.post(url, json=flow) as response:
        text = await response.text()

        if response.status not in (200, 201):
            raise Exception(
                f"Error instalando flow {flow['name']}: "
                f"HTTP {response.status} - {text}"
            )


async def build_route(
    controller_ip,
    route,
    src_mac,
    dst_mac,
    src_ip,
    dst_ip,
    protocol,
    dst_port,
    handler
):
    transport_name, ip_proto = transport(protocol, dst_port)
    validate_route(route)
    valid_handler(handler)
    valid_mac(src_mac)
    valid_mac(dst_mac)
    ipaddress.IPv4Address(src_ip)
    ipaddress.IPv4Address(dst_ip)
    flows = []

    for i in range(0, len(route), 2):
        ingress = route[i]
        egress = route[i + 1]

        dpid = ingress["switch"]
        in_port = port_number(ingress["port"])
        out_port = port_number(egress["port"])

        forward_flow = {
            "switch": dpid,
            "name": f"{handler}_fwd_{i//2}",
            "cookie": "0",
            "priority": "32768",
            "in_port": str(in_port),
            "eth_src": src_mac,
            "eth_dst": dst_mac,
            "eth_type": "0x0800",
            "ipv4_src": src_ip,
            "ipv4_dst": dst_ip,
            "ip_proto": ip_proto,
            f"{transport_name}_dst": str(dst_port),
            "active": "true",
            "actions": f"output={out_port}"
        }

        reverse_flow = {
            "switch": dpid,
            "name": f"{handler}_rev_{i//2}",
            "cookie": "0",
            "priority": "32768",
            "in_port": str(out_port),
            "eth_src": dst_mac,
            "eth_dst": src_mac,
            "eth_type": "0x0800",
            "ipv4_src": dst_ip,
            "ipv4_dst": src_ip,
            "ip_proto": ip_proto,
            f"{transport_name}_src": str(dst_port),
            "active": "true",
            "actions": f"output={in_port}"
        }

        arp_forward = {
            "switch": dpid,
            "name": f"{handler}_arp_fwd_{i//2}",
            "cookie": "0",
            "priority": "32767",
            "in_port": str(in_port),
            "eth_type": "0x0806",
            "active": "true",
            "actions": f"output={out_port}"
        }

        arp_reverse = {
            "switch": dpid,
            "name": f"{handler}_arp_rev_{i//2}",
            "cookie": "0",
            "priority": "32767",
            "in_port": str(out_port),
            "eth_type": "0x0806",
            "active": "true",
            "actions": f"output={in_port}"
        }

        flows.extend([
            forward_flow,
            reverse_flow,
            arp_forward,
            arp_reverse
        ])

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            push_flow(session, controller_ip, flow)
            for flow in flows
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise RuntimeError(f"{len(errors)} operaciones fallaron. Primera causa: {errors[0]}. "
                               "Puede haber flows parciales; conserve el handler para borrarlos.")

    print(f"\nConexion {handler} creada correctamente.")
    print(f"Flows instalados: {len(flows)}")


async def delete_flow(session, controller_ip, flow_name):
    url = f"http://{controller_ip}:8080/wm/staticflowpusher/json"

    async with session.delete(
        url,
        json={"name": flow_name}
    ) as response:
        text = await response.text()

        if response.status not in (200, 201):
            raise Exception(
                f"Error eliminando {flow_name}: "
                f"HTTP {response.status} - {text}"
            )


async def delete_connection(controller_ip, handler, hops):
    valid_handler(handler)
    if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
        raise ValueError("La cantidad de switches debe ser un entero positivo.")
    names = []

    for i in range(hops):
        names.extend([
            f"{handler}_fwd_{i}",
            f"{handler}_rev_{i}",
            f"{handler}_arp_fwd_{i}",
            f"{handler}_arp_rev_{i}"
        ])

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        tasks = [
            delete_flow(session, controller_ip, name)
            for name in names
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise RuntimeError(f"{len(errors)} operaciones fallaron. Primera causa: {errors[0]}. "
                               "Puede haber flows parciales; conserve el handler para borrarlos.")

    print(f"\nConexion {handler} eliminada.")
    print(f"Flows eliminados: {len(names)}")


def authorized_courses(courses, student_name, server_name, service_name):
    """Politica del laboratorio: matricula + curso ACTIVE + servidor/servicio."""
    return [course for course in courses
            if course.state == "ACTIVE" and student_name in course.students
            and any(p["name"] == server_name and service_name in p["allowed_services"]
                    for p in course.servers)]


def choose(items, label, describe=lambda item: item.name):
    if not items:
        raise ValueError(f"No hay {label} disponibles.")
    print(f"\nSeleccionar {label}:")
    for index, item in enumerate(items, 1):
        print(f"  {index}. {describe(item)}")
    answer = input("Numero (0 para cancelar): ").strip()
    if answer == "0":
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(items):
        raise ValueError("Seleccione un numero de la lista.")
    return items[int(answer) - 1]


def ask_menu(title, options):
    print(f"\n--- {title} ---")
    for key, label in options:
        print(f"{key}. {label}")
    return input("Opcion: ").strip()


class Application:
    """Estado de la CLI; las funciones SDN originales siguen siendo reutilizables."""

    def __init__(self, database, controller_ip="127.0.0.1"):
        self.database = Path(database).expanduser().resolve()
        self.controller_ip = controller_ip
        self.students, self.servers, self.courses = load_data(self.database)
        self.registry = self.database.with_suffix(".connections.json")
        self.connections = {}
        self.dirty = False
        if self.registry.exists():
            with self.registry.open(encoding="utf-8") as file:
                self.connections = json.load(file)
            if not isinstance(self.connections, dict):
                raise ValueError("Registro de conexiones invalido; conserve el archivo para recuperarlo.")
            for handler, connection in self.connections.items():
                valid_handler(handler)
                if not isinstance(connection, dict) or not all(
                    k in connection for k in ("student", "server", "service", "controller", "hops", "state")
                ):
                    raise ValueError(f"Registro incompleto para {handler}.")
        for student in self.students:
            try:
                valid_mac(student.mac)
            except ValueError as error:
                print(f"Aviso ({student.name}): {error}")

    def save_connections(self):
        temporary = self.registry.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.connections, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.registry)

    def export_data(self):
        filename = input(f"Archivo de salida [{self.database}]: ").strip()
        destination = Path(filename).expanduser() if filename else self.database
        if destination.exists() and input("El archivo existe. Reemplazar (s/N): ").strip().lower() != "s":
            return
        data = {
            "students": [dict(name=s.name, mac=s.mac, **({"code": s.code} if s.code else {}))
                         for s in self.students],
            "servers": [dict(name=s.name, ip=s.ip,
                             services=[vars(service) for service in s.services]) for s in self.servers],
            "courses": [vars(course) for course in self.courses],
        }
        with destination.open("w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
        self.dirty = False
        print(f"Datos exportados a {destination}.")

    def import_data(self):
        filename = input("Archivo YAML a importar: ").strip()
        if self.dirty and input("Descartar cambios sin exportar (s/N): ").strip().lower() != "s":
            return
        students, servers, courses = load_data(filename)
        # No permitir que una importacion deje conexiones sin sus identidades o permisos.
        for connection in self.connections.values():
            if not authorized_courses(courses, connection["student"], connection["server"], connection["service"]):
                raise ValueError("Borre las conexiones afectadas antes de importar datos que retiren sus permisos.")
        self.students, self.servers, self.courses = students, servers, courses
        self.dirty = True
        print("Datos importados. Use Exportar para guardarlos en el archivo de trabajo.")

    def course_menu(self):
        while True:
            option = ask_menu("Cursos", [("1", "Listar"), ("2", "Mostrar detalles"),
                ("3", "Gestionar alumnos"), ("0", "Volver")])
            if option == "0":
                return
            if option == "1":
                for c in self.courses:
                    print(f"{c.code or '-'} | {c.name} | {c.state}")
            elif option in ("2", "3"):
                course = choose(self.courses, "curso", lambda c: f"{c.code or '-'} - {c.name}")
                if course is None:
                    continue
                if option == "2":
                    print(f"Codigo: {course.code or 'No registrado'}\nNombre: {course.name}\nEstado: {course.state}")
                    print("Alumnos: " + (", ".join(course.students) or "Ninguno"))
                    for p in course.servers:
                        print(f"{p['name']}: {', '.join(p['allowed_services'])}")
                else:
                    action = ask_menu("Gestionar alumnos", [("1", "Agregar"), ("2", "Eliminar"), ("0", "Volver")])
                    if action == "0":
                        continue
                    if action not in ("1", "2"):
                        raise ValueError("Opcion invalida.")
                    available = self.students if action == "1" else [s for s in self.students if s.name in course.students]
                    student = choose(available, "alumno")
                    if student is None:
                        continue
                    if action == "1":
                        course.add_student(student.name)
                    else:
                        remaining = [c for c in self.courses if c is not course]
                        for conn in self.connections.values():
                            if conn["student"] == student.name and not authorized_courses(
                                remaining, student.name, conn["server"], conn["service"]
                            ):
                                raise ValueError("Borre primero las conexiones que dependen de esta matricula.")
                        course.remove_student(student.name)
                    self.dirty = True
                    print("Matricula actualizada. Use Exportar para guardar los cambios.")
            else:
                print("Opcion invalida.")

    def student_menu(self):
        while True:
            option = ask_menu("Alumnos", [("1", "Crear"), ("2", "Listar / filtrar por nombre"),
                ("3", "Listar por curso"), ("4", "Mostrar detalles"),
                ("5", "Actualizar MAC"), ("0", "Volver")])
            if option == "0":
                return
            if option == "1":
                name = input("Nombre: ").strip()
                mac = valid_mac(input("MAC: ").strip())
                if not name or any(s.name.casefold() == name.casefold() or s.mac.lower() == mac for s in self.students):
                    raise ValueError("Nombre vacio o nombre/MAC ya registrado.")
                code = input("Codigo (opcional): ").strip() or None
                self.students.append(Student(name, mac, code))
                self.dirty = True
                print(f"Alumno creado: {name}. Use Cursos para matricularlo y Exportar para guardar.")
            elif option in ("2", "3"):
                if option == "2":
                    query = input("Filtro de nombre (Enter = todos): ").strip().casefold()
                    selected = [s for s in self.students if query in s.name.casefold()]
                else:
                    course = choose(self.courses, "curso", lambda c: f"{c.code or '-'} - {c.name}")
                    if course is None:
                        continue
                    selected = [s for s in self.students if s.name in course.students]
                for s in selected:
                    print(f"{s.code or '-'} | {s.name} | {s.mac}")
                if not selected:
                    print("Sin alumnos que coincidan.")
            elif option in ("4", "5"):
                student = choose(self.students, "alumno")
                if student is None:
                    continue
                if option == "4":
                    print(f"Codigo: {student.code or 'No registrado en YAML'}\nNombre: {student.name}\nMAC: {student.mac}")
                    print("Cursos: " + (", ".join(c.code or c.name for c in self.courses if student.name in c.students) or "Ninguno"))
                else:
                    if any(c["student"] == student.name for c in self.connections.values()):
                        raise ValueError("Borre primero las conexiones del alumno antes de cambiar su MAC.")
                    mac = valid_mac(input("Nueva MAC: ").strip())
                    if any(s is not student and s.mac.lower() == mac for s in self.students):
                        raise ValueError("MAC ya registrada por otro alumno.")
                    student.mac = mac
                    self.dirty = True
                    print("MAC actualizada. Use Exportar para guardar.")
            else:
                print("Opcion invalida.")

    def server_menu(self):
        while True:
            option = ask_menu("Servidores", [("1", "Listar"), ("2", "Mostrar detalles y servicios"), ("0", "Volver")])
            if option == "0":
                return
            if option == "1":
                for server in self.servers:
                    print(f"{server.name} | {server.ip}")
            elif option == "2":
                server = choose(self.servers, "servidor")
                if server:
                    print(f"Nombre: {server.name}\nIP: {server.ip}")
                    for service in server.services:
                        print(f"  {service.name}: {service.protocol}/{service.port}")
            else:
                print("Opcion invalida.")

    def policy_menu(self):
        server = choose(self.servers, "servidor")
        if server is None:
            return
        service = choose(server.services, "servicio")
        if service is None:
            return
        matches = [c for c in self.courses if c.state == "ACTIVE" and any(
            p["name"] == server.name and service.name in p["allowed_services"] for p in c.servers)]
        print(f"Cursos activos con acceso a {server.name} / {service.name}:")
        for course in matches:
            print(f"{course.code or '-'} | {course.name}")
        if not matches:
            print("Ninguno.")

    def select_access(self):
        student = choose(self.students, "alumno")
        if student is None:
            return None
        server = choose(self.servers, "servidor")
        if server is None:
            return None
        service = choose(server.services, "servicio", lambda s: f"{s.name} ({s.protocol}/{s.port})")
        if service is None:
            return None
        if not authorized_courses(self.courses, student.name, server.name, service.name):
            raise ValueError("Acceso denegado: el alumno no tiene un curso ACTIVE que permita este servicio.")
        return student, server, service

    def create_connection(self):
        access = self.select_access()
        if access is None:
            return
        student, server, service = access
        valid_mac(student.mac)
        handler = valid_handler(input("Handler unico (ej. tom_server3_ssh): ").strip())
        if handler in self.connections:
            raise ValueError("Handler ya registrado; borre la conexion anterior antes de reutilizarlo.")
        if any(c["student"] == student.name and c["server"] == server.name
               and c["service"] == service.name and c["controller"] == self.controller_ip
               for c in self.connections.values()):
            raise ValueError("Ya existe una conexion registrada para este alumno y servicio.")
        src_ip = str(ipaddress.IPv4Address(input("IP del alumno en la red SDN (ej. 10.0.0.1): ").strip()))
        dst_mac = valid_mac(input(f"MAC SDN de {server.name} ({server.ip}): ").strip())
        route = get_route(self.controller_ip, student.mac, dst_mac)
        record = dict(student=student.name, server=server.name, service=service.name,
                      controller=self.controller_ip, hops=len(route) // 2, route=route,
                      src_ip=src_ip, dst_ip=server.ip, src_mac=student.mac, dst_mac=dst_mac,
                      protocol=service.protocol, port=service.port, state="PENDIENTE")
        # Registrar ANTES de enviar: permite limpiar una instalacion parcial tras un fallo.
        self.connections[handler] = record
        try:
            self.save_connections()
        except OSError:
            del self.connections[handler]
            raise
        try:
            asyncio.run(build_route(self.controller_ip, route, student.mac, dst_mac,
                                   src_ip, server.ip, service.protocol, service.port, handler))
        except Exception:
            record["state"] = "ERROR: posible instalacion parcial"
            self.save_connections()
            raise
        record["state"] = "INSTALADA (solicitud aceptada por el controlador)"
        self.save_connections()

    def remove_connection(self):
        handler = valid_handler(input("Handler a borrar: ").strip())
        connection = self.connections.get(handler)
        if connection is None:
            print("Handler no registrado. Para borrar una conexion de las pruebas anteriores, indique sus datos.")
            access = self.select_access()
            if access is None:
                return
            student, server, service = access
            hops = int(input("Cantidad de switches de la ruta original (ej. 3): ").strip())
            if hops < 1:
                raise ValueError("Cantidad de switches invalida.")
            connection = dict(student=student.name, server=server.name, service=service.name,
                              controller=self.controller_ip, hops=hops, state="RECUPERADA")
        if not authorized_courses(self.courses, connection["student"], connection["server"], connection["service"]):
            raise ValueError("Borrado denegado: el alumno no esta autorizado segun las politicas actuales.")
        print(f"{handler}: {connection['student']} -> {connection['server']} / {connection['service']}")
        if input("Eliminar esta conexion y sus flows (s/N): ").strip().lower() != "s":
            return
        self.connections[handler] = connection
        connection["state"] = "BORRADO PENDIENTE"
        self.save_connections()
        asyncio.run(delete_connection(connection["controller"], handler, connection["hops"]))
        del self.connections[handler]
        self.save_connections()

    def connection_menu(self):
        while True:
            option = ask_menu("Conexiones", [("1", "Crear"), ("2", "Listar"),
                ("3", "Mostrar detalles / ruta"), ("4", "Borrar por handler"), ("0", "Volver")])
            if option == "0":
                return
            if option == "1":
                self.create_connection()
            elif option == "2":
                print("Registro local de conexiones manuales (no consulta el estado actual de los switches):")
                for handler, c in self.connections.items():
                    print(f"{handler} | {c['student']} -> {c['server']} / {c['service']} | {c['state']}")
                if not self.connections:
                    print("Sin conexiones registradas.")
            elif option == "3":
                handler = input("Handler: ").strip()
                if handler not in self.connections:
                    raise ValueError("Handler no registrado.")
                print(json.dumps(self.connections[handler], indent=2, ensure_ascii=False))
            elif option == "4":
                self.remove_connection()
            else:
                print("Opcion invalida.")

    def run(self):
        actions = {"1": self.import_data, "2": self.export_data, "3": self.course_menu,
                   "4": self.student_menu, "5": self.server_menu, "6": self.policy_menu,
                   "7": self.connection_menu}
        print(f"Laboratorio 4 - Aplicacion SDN\nBase de datos: {self.database}\nControlador: {self.controller_ip}:8080")
        while True:
            try:
                option = ask_menu("Menu principal", [("1", "Importar"), ("2", "Exportar"),
                    ("3", "Cursos"), ("4", "Alumnos"), ("5", "Servidores"),
                    ("6", "Politicas"), ("7", "Conexiones"), ("8", "Salir")])
                if option in ("8", "0"):
                    if self.dirty and input("Hay cambios sin exportar. Salir sin guardarlos (s/N): ").strip().lower() != "s":
                        continue
                    print("Hasta luego. Las conexiones existentes permanecen en el controlador.")
                    return
                if option in actions:
                    actions[option]()
                else:
                    print("Opcion invalida.")
            except (EOFError, KeyboardInterrupt):
                print("\nSalida. Los cambios de datos sin exportar no se guardaron; los flows no se borraron.")
                return
            except (ValueError, KeyError, TypeError, OSError, RuntimeError,
                    requests.RequestException, aiohttp.ClientError, yaml.YAMLError) as error:
                print(f"Error: {error}")


def menu(students=None, servers=None, courses=None, controller_ip="127.0.0.1", database=None):
    """Entrada de la CLI. menu() carga el YAML situado junto al script."""
    app = Application(database or Path(__file__).with_name("database.yaml"), controller_ip)
    if students is not None and servers is not None and courses is not None:
        app.students, app.servers, app.courses = students, servers, courses
    app.run()


def main():
    parser = argparse.ArgumentParser(description="Laboratorio 4 - CLI SDN con Floodlight")
    parser.add_argument("--database", default=str(Path(__file__).with_name("database.yaml")), help="Archivo YAML")
    parser.add_argument("--controller", default="127.0.0.1", help="IP o nombre del controlador (REST en puerto 8080)")
    args = parser.parse_args()
    try:
        menu(controller_ip=args.controller, database=args.database)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        parser.exit(1, f"No se pudo cargar la aplicacion: {error}\n")


if __name__ == "__main__":
    main()
