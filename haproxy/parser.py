import re
import os
import urlparse


def parse_uuid_from_resource_uri(uri):
    terms = uri.strip("/").split("/")
    if len(terms) < 2:
        return ""
    return terms[-1]


class Specs(object):
    service_alias_match = re.compile(r"_PORT_\d{1,5}_(TCP|UDP)$")

    def __init__(self, tutum_haproxy_container=None, tutum_haproxy_service=None):
        self.envvars = self._parse_envvars(tutum_haproxy_container)
        self.service_aliases = self._parser_service_aliases(tutum_haproxy_service)
        self.details = self._parse_specs()
        self.routes = self._parse_routes(tutum_haproxy_container)
        self.vhosts = self._parse_vhosts()

    def _parse_envvars(self, tutum_haproxy_container):
        envvars = {}
        if tutum_haproxy_container:
            for pair in tutum_haproxy_container.container_envvars:
                envvars[pair['key']] = pair['value']
        else:
            envvars = os.environ
        return envvars

    def _parser_service_aliases(self, tutum_haproxy_services):
        if tutum_haproxy_services:
            service_aliases = [service["name"].upper().replace("-", "_")
                               for service in tutum_haproxy_services.linked_to_service]
        else:
            service_aliases = []
            for key, value in self.envvars.iteritems():
                match = Specs.service_alias_match.search(key)
                if match:
                    service_aliases.append(key[:match.start()])
        return service_aliases

    def _parse_specs(self):
        env_parser = EnvParser(self.service_aliases)
        for key, value in self.envvars.iteritems():
            env_parser.parse(key, value)
        return env_parser.get_spec()

    def _parse_routes(self, tutum_haproxy_container):
        return RouteParser.parse(self.details, tutum_haproxy_container)

    def _parse_vhosts(self):
        vhosts = []
        for service_alias, attr in self.details.iteritems():
            virtual_hosts = attr["virtual_host"]

            if virtual_hosts:
                for vhost in virtual_hosts:
                    vhost["service_alias"] = service_alias
                    vhosts.append(vhost)
        return vhosts

    def get_specs(self):
        return self.details

    def get_routes(self):
        return self.routes

    def get_vhosts(self):
        return self.vhosts

    def get_default_ssl_cert(self):
        if not hasattr(self, "default_ssl_cert"):
            self.default_ssl_cert = filter(lambda x: x, [attr["default_ssl_cert"] for attr in self.details.itervalues()])
        return self.default_ssl_cert

    def get_ssl_cert(self):
        if not hasattr(self, "ssl_cert"):
            self.ssl_cert = filter(lambda x: x, [attr["ssl_cert"] for attr in self.details.itervalues()])
        return self.ssl_cert

    def get_force_ssl(self):
        if not hasattr(self, "force_ssl"):
            self.force_ssl = []
            for container_alias, attr in self.details.iteritems():
                if attr["force_ssl"]:
                    self.force_ssl.append(container_alias)
        return self.force_ssl


class RouteParser(object):
    backend_match = re.compile(r"(?P<proto>tcp|udp):\/\/(?P<addr>[^:]*):(?P<port>.*)")
    service_alias_match = re.compile(r"_PORT_\d{1,5}_(TCP|UDP)$")

    @staticmethod
    def parse(specs, tutum_haproxy_container=None):
        if tutum_haproxy_container:
            return RouteParser.parse_tutum_routes(specs, tutum_haproxy_container.linked_to_container)
        else:
            return RouteParser.parse_local_routes(specs, os.environ)

    @staticmethod
    def parse_tutum_routes(specs, container_links):
        # Input:  settings        = {'HELLO_1': {'exclude_ports': ['3306']}}
        #         container_links = [{"endpoints": {"80/tcp": "tcp://10.7.0.3:80", "3306/tcp": "tcp://10.7.0.8:3306"},
        #                             "name": "hello-1",
        #                             "from_container": "/api/v1/container/702d18d4-7934-4715-aea3-c0637f1a4129/",
        #                             "to_container": "/api/v1/container/60b850b7-593e-461b-9b61-5fe1f5a681aa/"},
        #                            {"endpoints": {"80/tcp": "tcp://10.7.0.5:80"},
        #                              "name": "hello-2",
        #                              "from_container": "/api/v1/container/702d18d4-7934-4715-aea3-c0637f1a4129/",
        #                              "to_container": "/api/v1/container/65b18c61-b551-4c7f-a92b-06ef95494d5a/"}]
        # Output: links           = {'HELLO': [{'proto': 'tcp', 'addr': '10.7.0.3', 'port': '80'},
        #                                      {'proto': 'tcp', 'addr': '10.7.0.5', 'port': '80'}]
        routes = {}
        for container_link in container_links:
            container_name = container_link.get("name").upper().replace("-", "_")
            pos = container_name.rfind("_")
            if pos > 0:
                service_alias = container_name[:pos]
                for _, value in container_link.get("endpoints", {}).iteritems():
                    route = RouteParser.backend_match.match(value).groupdict()
                    route.update({"container_name": container_name})
                    exclude_ports = specs.get(service_alias, {}).get("exclude_ports")
                    if not exclude_ports or (exclude_ports and route["port"] not in exclude_ports):
                        if service_alias in routes:
                            routes[service_alias].append(route)
                        else:
                            routes[service_alias] = [route]
        return routes

    @staticmethod
    def parse_local_routes(specs, envvars):
        # Input:  settings = {'HELLO_1': {'exclude_ports': [3306]}}
        #         envvars  = {'HELLO_1_PORT_80_TCP': 'tcp://172.17.0.30:80',
        #                    'HELLO_2_PORT_80_TCP': 'tcp://172.17.0.31:80',
        #                    'HELLO_1_PORT_3306_TCP': 'tcp://172.17.0.30:3306',
        #                    'HELLO_2_PORT_3306_TCP': 'tcp://172.17.0.31:3306'}
        # Output: routes   = {'HELLO_2': [{'proto': 'tcp', 'port': '3306', 'addr': '172.17.0.31'},
        #                                {'proto': 'tcp', 'port': '80', 'addr': '172.17.0.31'}],
        #                    'HELLO_1': [{'proto': 'tcp', 'port': '80', 'addr': '172.17.0.30'}]}
        routes = {}
        for key, value in envvars.iteritems():
            if not key or not value:
                continue
            alias_match = RouteParser.service_alias_match.search(key)
            if alias_match:
                service_alias = key[:alias_match.start()]
                be_match = RouteParser.backend_match.match(value)
                if be_match:
                    route = RouteParser.backend_match.match(value).groupdict()
                    route.update({"container_name": service_alias})
                    exclude_ports = specs.get(service_alias, {}).get("exclude_ports")
                    if not exclude_ports or (exclude_ports and route["port"] not in exclude_ports):
                        if service_alias in routes:
                            routes[service_alias].append(route)
                        else:
                            routes[service_alias] = [route]
        return routes


class EnvParser(object):
    service_alias_match = re.compile(r"_ENV_")

    def __init__(self, service_aliases):
        self.service_aliases = service_aliases
        self.specs = {}

    def parse(self, key, value):
        for method in self.__class__.__dict__:
            if method.startswith("parse_"):
                match = EnvParser.service_alias_match.search(key)
                if match:
                    container_alias = key[:match.start()]
                    for service_alias in self.service_aliases:
                        if container_alias.startswith(service_alias):
                            attr_name = method[6:]
                            if key.endswith("_ENV_%s" % attr_name.upper()):
                                attr_value = getattr(self, method)(value)
                            else:
                                attr_value = None

                            if service_alias in self.specs:
                                if attr_name in self.specs[service_alias]:
                                    if attr_value:
                                        self.specs[service_alias][attr_name] = attr_value
                                else:
                                    self.specs[service_alias][attr_name] = attr_value
                            else:
                                self.specs[service_alias] = {attr_name: attr_value}

    def get_spec(self):
        return self.specs

    @staticmethod
    def parse_default_ssl_cert(value):
        return value.replace(r'\n', '\n')

    @staticmethod
    def parse_ssl_cert(value):
        return value.replace(r'\n', '\n')

    @staticmethod
    def parse_exclude_ports(value):
        # '3306, 8080' => ['3306', '8080']
        return [x.strip() for x in value.strip().split(",")]

    @staticmethod
    def parse_virtual_host(value):
        # 'http://a.com:8080, https://b.com, c.com'  = >
        #   [{'path': '', 'host': 'a.com', 'scheme': 'http', 'port': '8080'},
        #    {'path': '', 'host': 'b.com', 'scheme': 'https', 'port': '443'},
        #    {'path': '', 'host': 'c.com', 'scheme': 'http', 'port': '80'}]
        vhosts = []
        for h in [h.strip() for h in value.strip().split(",")]:
            pr = urlparse.urlparse(h)
            if not pr.netloc:
                pr = urlparse.urlparse("http://%s" % h)
            port = '443' if pr.scheme.lower() in ['https', 'wss'] else "80"
            host = pr.netloc
            if ":" in pr.netloc:
                host_port = pr.netloc.split(":")
                host = host_port[0]
                port = host_port[1]
            vhosts.append({"scheme": pr.scheme,
                           "host": host,
                           "port": port,
                           "path": pr.path})
        return vhosts

    @staticmethod
    def parse_force_ssl(value):
        return value

    @staticmethod
    def parse_appsession(value):
        return value

    @staticmethod
    def parse_balance(value):
        return value
