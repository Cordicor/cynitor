"""The scanner's dealings with the services nodes serve: clients for them, calls, and what a call on the bus says.

A client is made for every service a node's registers name (add_servers); the
dashboard's Services tab is built from them (get_service_schema,
get_client_info) and calls through them (make_service_call). Calls made by
anyone on the bus are read for the recordings (describe_service_transfer).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any, Optional

import pycyphal.presentation
from pycyphal.dsdl import deserialize, to_builtin
from pydsdl import CompositeType


class ScannerServices:
    """The service part of ScannerNode, which has the pycyphal node (``_node``), the
    nodes seen (``all_nodes``) and the tables of clients and service types these use."""

    async def add_servers(self, node_id: int, dsdl_srv_messages: dict[int, str]) -> dict[int, dict[str, Any]]:
        """
        Creates clients for the services specified in dsdl_srv_messages for the given node_id.
        
        Args:
            node_id (int): The ID of the node to create service clients for.
            dsdl_srv_messages (Dict[int, str]): Dictionary mapping service IDs to service type names.
        """
        service_info_list = {}

        for service_id, service_type in dsdl_srv_messages.items():
            last_dot_index = service_type.rfind('.')
            if last_dot_index == -1:
                logging.error(f"Invalid service type format for service_id {service_id}: {service_type}")
                continue

            namespace = service_type[:last_dot_index]
            service_name = service_type[last_dot_index + 1:]
            client_key = (node_id, service_id)

            try:
                # Import the service module and class
                module = importlib.import_module(namespace)
                service_class = getattr(module, service_name)

                # Only create if not already cached
                if client_key not in self.service_clients:
                    client = self._node.make_client(service_class, node_id, service_id)
                    if not client:
                        logging.error(f"Failed to create client for service {service_type} (ID: {service_id}) on node {node_id}")
                        continue
                    self.service_clients[client_key] = client
                    logging.debug(f"Created and cached client for service {service_type} (ID: {service_id}) on node {node_id}")
                else:
                    logging.debug(f"Using cached client for service {service_type} (ID: {service_id}) on node {node_id}")

                self.service_metadata[client_key] = {
                    "namespace": namespace,
                    "service_name": service_name,
                }

                if service_name in self.services:
                    logging.debug(f"Service type {service_type} already exists, adding service_id {service_id} for node {node_id}")
                    service_info_list[service_id] = {
                        "name": service_name,
                        "node_id": node_id,
                        "attributes": None
                    }
                    continue
                else:
                    self.services.add(service_name)

                service_info_list[service_id] = {
                "name": service_name,
                "node_id": node_id,
                "attributes": {}
                }


                for attribute in service_class.Request._MODEL_.attributes:
                    attr_name = attribute.name
                    attr_type = str(attribute.data_type)
                    service_info_list[service_id]["attributes"][attr_name] = attr_type

            except Exception as e:
                logging.error(f"Failed to create client for service {service_type} (ID: {service_id}) on node {node_id}: {e}")
                if client_key not in self.service_metadata:
                    self.service_metadata[client_key] = {
                        "namespace": namespace,
                        "service_name": service_name,
                        "unavailable": True,
                    }

        return service_info_list

    async def make_service_call(self, node_id: int, service_id: int, service_type: str, attributes: dict[str, dict[str, Any]]) -> str:
        """
        Makes a service call to the specified node and service with given attributes.

        Args:
        node_id (int): ID of the target node.
        service_id (int): ID of the service .
        service_type (str): Service type (e.g., 'SumService_1_0').
        attributes (Dict[str, Dict[str, Any]]): Dictionary of {attr_name: {value: Any, type: Optional[str]}}.

        Returns:
        str: String representation of the service response.

        Raises:
        ValueError: If service_type is invalid, attributes are empty/incorrect, or client is missing.
        Exception: If the service call fails.
        """
        logging.debug(f"Attempting service call: node_id={node_id}, service_id={service_id}, service_type={service_type}, attributes={attributes}")

        try:
            client = self.service_clients.get((node_id, service_id))
            if not client:
                logging.error(f"No client found for service {service_id} on node {node_id}")
                raise ValueError(f"No client found for service {service_id} on node {node_id}")
            logging.debug(f"Using cached client for service {service_id} on node {node_id}")

            request = self._build_request(client, service_type, attributes)

            logging.info(f"Sending request for {service_type} on node {node_id}, service {service_id}:")
            for attr_name in [a.name for a in request._MODEL_.attributes]:
                logging.info(f"  {attr_name}: {getattr(request, attr_name)} (type: {type(getattr(request, attr_name)).__name__})")

            # pycyphal's client gives up after its own 1 s unless told otherwise.
            client.response_timeout = self.SERVICE_CALL_TIMEOUT
            try:
                response_tuple = await asyncio.wait_for(client.call(request), timeout=self.SERVICE_CALL_TIMEOUT + 1)
                logging.debug(f"Received response tuple for service {service_id}: {response_tuple}")
            except pycyphal.presentation.PortClosedError:
                # The node was dropped (gone offline, or restarted) while the call waited.
                raise ConnectionError(f"Node {node_id} went offline while the call waited for its answer") from None
            except asyncio.TimeoutError:
                response_tuple = None

            if response_tuple is None or response_tuple[0] is None:
                logging.error(f"Service {service_id} on node {node_id}: no response in {self.SERVICE_CALL_TIMEOUT}s")
                raise asyncio.TimeoutError(f"Service {service_id} on node {node_id} gave no answer "
                                           f"in {self.SERVICE_CALL_TIMEOUT:.0f}s")

            response = response_tuple[0]
            logging.info(f"Received response for service {service_id}: {response}")

            # JSON, so that it reads well and scripts can parse it.
            response_str = json.dumps(to_builtin(response), indent=2)
            logging.debug(f"Formatted response as JSON: {response_str}")
            return response_str

        except Exception as e:
            logging.error(f"Error making service call for service {service_id} on node {node_id}: {str(e)}")
            raise

    def _build_request(self, client, service_type: str, attributes: dict[str, dict[str, Any]]):
        """The request to send through ``client``, with the fields ``attributes`` gives set; the rest as the type has them.

        The request must be built from the SAME class the client was created
        with. Re-importing the module here can yield a different class object
        (e.g. after a custom-DSDL recompile drops the module from
        sys.modules), which makes client.call() reject the request with a
        confusing "expected X, got X" identity mismatch. Sourcing Request from
        client.dtype avoids that.
        """
        request_class = getattr(client.dtype, "Request", None)
        if not request_class:
            logging.error(f"No Request class found for service {service_type}")
            raise ValueError(f"No Request class found for service {service_type}")
        request = request_class()

        for attr in request._MODEL_.attributes:
            logging.debug(f"Processing attribute: name={attr.name}, data_type={str(attr.data_type)}")
            try:
                composite = isinstance(attr.data_type, CompositeType)
                kind = "composite" if composite else "primitive"
                logging.debug(f"Attribute {attr.name} is a {kind} type: {str(attr.data_type)}")
                for key in attributes:
                    if str(key) != str(attr.name):
                        continue
                    logging.debug(f"Matched attribute key: {key}")
                    try:
                        if composite:
                            self._set_composite_field(request, attr, key, attributes.get(key, {}))
                        else:
                            self._set_primitive_field(request, attr, key, attributes.get(key, {}))
                    except Exception as e:
                        logging.error(f"Error processing attribute {key} for {kind} type {attr.name}: {str(e)}")
                        raise ValueError(f"Error processing attribute {key}: {str(e)}")
            except Exception as e:
                logging.error(f"Failed to process attribute {attr.name}: {str(e)}")
                raise ValueError(f"Error processing attribute {attr.name}: {str(e)}")
        return request

    @staticmethod
    def _set_composite_field(request, attr, key: str, attr_data: dict[str, Any]) -> None:
        """Set the request's field ``attr``, of a composite type, as ``attr_data`` ({type, value}) gives it."""
        attr_type = attr_data.get('type')
        attr_value = attr_data.get('value')
        if attr_type is None or attr_value is None:
            logging.error(f"Missing type or value for attribute {key}: type={attr_type}, value={attr_value}")
            raise ValueError(f"Attribute {key} missing 'type' or 'value'")
        logging.debug(f"Attribute {key}: type={attr_type}, value={attr_value}")

        # "ns.Name.1.0" names the class Name_1_0 of the module ns.
        try:
            attribute_type_split = attr_type.split('.')
            if len(attribute_type_split) < 3:
                logging.error(f"Invalid type format for attribute {key}: {attr_type}")
                raise ValueError(f"Invalid type format: {attr_type}")
            attribute_type_name = '_'.join(attribute_type_split[-3:])
            attribute_type_namespace = '.'.join(attribute_type_split[:-3])
            logging.debug(f"Parsed type: namespace={attribute_type_namespace}, type_name={attribute_type_name}")
        except AttributeError as e:
            logging.error(f"Failed to split type {attr_type} for attribute {key}: {str(e)}")
            raise ValueError(f"Invalid type string {attr_type}: {str(e)}")

        try:
            module = importlib.import_module(attribute_type_namespace)
            attribute_class = getattr(module, attribute_type_name)
            logging.debug(f"Imported class {attribute_type_name} from {attribute_type_namespace}")
        except (ImportError, AttributeError) as e:
            logging.error(f"Failed to import {attribute_type_name} from {attribute_type_namespace}: {str(e)}")
            raise ValueError(f"Cannot import {attribute_type_name}: {str(e)}")

        try:
            struct_instance = attribute_class()
            fields = struct_instance._MODEL_.fields
            if not fields:
                logging.error(f"No fields found in {attribute_type_name} for attribute {key}")
                raise ValueError(f"No fields defined in {attribute_type_name}")

            # A dict names the fields to set; a single value sets the
            # first field, as single-field wrappers (primitive.String) need.
            values = attr_value if isinstance(attr_value, dict) else {fields[0].name: attr_value}
            known = {f.name for f in fields}
            for field_name, field_value in values.items():
                if field_name not in known:
                    raise ValueError(f"{attribute_type_name} has no field {field_name!r}")
                try:
                    setattr(struct_instance, field_name, field_value)
                except (TypeError, ValueError) as e:
                    logging.error(f"Failed to set {field_name}={field_value} on {attribute_type_name}: {str(e)}")
                    raise ValueError(f"Invalid value {field_value} for field {field_name}: {str(e)}")

            setattr(request, attr.name, struct_instance)
            logging.debug(f"Assigned {attribute_type_name} instance to request attribute {attr.name}")
        except Exception as e:
            logging.error(f"Failed to create or configure {attribute_type_name} for attribute {key}: {str(e)}")
            raise ValueError(f"Error processing composite attribute {key}: {str(e)}")

    @staticmethod
    def _set_primitive_field(request, attr, key: str, attr_data: dict[str, Any]) -> None:
        """Set the request's field ``attr``, of a primitive type, to the value ``attr_data`` gives."""
        attr_value = attr_data.get('value')
        if attr_value is None:
            logging.error(f"Missing value for attribute {key}")
            raise ValueError(f"Attribute {key} missing 'value'")
        logging.debug(f"Attribute {key}: value={attr_value}")

        try:
            if str(attr.data_type) == "saturated float16":
                parsed_value = float(attr_value)  # PyCyphal coerces to float16
                logging.debug(f"Converted {attr_value} to float: {parsed_value}")
            else:
                parsed_value = attr_value  # Keep as-is for other types
            setattr(request, attr.name, parsed_value)
            logging.debug(f"Set request attribute {attr.name}={parsed_value} (type: {type(parsed_value).__name__})")
        except (TypeError, ValueError) as e:
            logging.error(f"Failed to set {attr.name}={attr_value} on request: {str(e)}")
            raise ValueError(f"Invalid value {attr_value} for attribute {attr.name}: {str(e)}")

    def describe_service_transfer(self, server_node_id: int, service_id: int, is_request: bool,
                                  payload: bytes) -> tuple[Optional[str], str]:
        """A service request or response heard on the bus, for the recordings:
        its type's name, when known, and its fields as JSON, or its bytes in
        hex where the type is unknown or they do not fit it."""
        name, service_class = self._service_class(server_node_id, service_id)
        if service_class is not None:
            try:
                value = deserialize(service_class.Request if is_request else service_class.Response,
                                    [memoryview(payload)])
                if value is not None:
                    return name, json.dumps(to_builtin(value))
            except Exception as e:
                logging.debug(f"Service {service_id} transfer does not read as {name}: {e}")
        return name, payload.hex(" ").upper()

    def _service_class(self, server_node_id: int, service_id: int) -> tuple[Optional[str], Any]:
        """The name and class of the service a node serves on ``service_id``, as
        its registers say, else as the fixed ID says; (None, None) if unknown."""
        meta = self.service_metadata.get((server_node_id, service_id))
        client = self.service_clients.get((server_node_id, service_id))
        if meta and client is not None:
            return f"{meta['namespace']}.{meta['service_name']}", client.dtype
        name = self.FIXED_SERVICES.get(service_id)
        if name is None:
            return None, None
        namespace, _, class_name = name.rpartition('.')
        try:
            return name, getattr(importlib.import_module(namespace), class_name)
        except (ImportError, AttributeError):
            return name, None

    def get_service_schema(self, node_id: int) -> list[dict[str, Any]]:
        """Return structured schema for all services on a node."""
        node = self.all_nodes.get(node_id)
        if not node or not node.has_appeared:
            return []

        services = []
        node_types = self.node_service_types.get(node_id, {})
        for sid in node.server_ServiceIDs:
            sid_int = int(sid)
            meta = self.service_metadata.get((node_id, sid_int))
            if not meta:
                type_str = node_types.get(sid_int) or self.STANDARD_SERVICES.get(sid_int)
                if type_str:
                    dot = type_str.rfind('.')
                    ns = type_str[:dot] if dot != -1 else None
                    sn = type_str[dot + 1:] if dot != -1 else type_str
                    try:
                        mod = importlib.import_module(ns)
                        svc_cls = getattr(mod, sn)
                        fields = []
                        for attr in svc_cls.Request._MODEL_.attributes:
                            fields.append(self._describe_field(attr))
                        services.append({
                            "service_id": sid_int,
                            "name": sn, "namespace": ns,
                            "full_type": type_str,
                            "callable": True,
                            "request_fields": fields,
                        })
                        continue
                    except Exception as e:
                        logging.warning(f"Cannot import {type_str} for service {sid_int} on node {node_id}: {e}")
                services.append({
                    "service_id": sid_int,
                    "name": None,
                    "namespace": None,
                    "full_type": type_str,
                    "callable": False,
                    "request_fields": None,
                })
                continue

            namespace = meta.get("namespace")
            service_name = meta.get("service_name")
            full_type = f"{namespace}.{service_name}" if namespace and service_name else None
            is_unavailable = meta.get("unavailable", False)

            if is_unavailable:
                services.append({
                    "service_id": sid_int,
                    "name": service_name,
                    "namespace": namespace,
                    "full_type": full_type,
                    "callable": False,
                    "request_fields": None,
                })
                continue

            fields = []
            try:
                module = importlib.import_module(namespace)
                service_class = getattr(module, service_name)
                for attr in service_class.Request._MODEL_.attributes:
                    fields.append(self._describe_field(attr))
            except Exception as e:
                logging.warning(f"Cannot introspect request fields for {full_type}: {e}")

            services.append({
                "service_id": sid_int,
                "name": service_name,
                "namespace": namespace,
                "full_type": full_type,
                "callable": True,
                "request_fields": fields,
            })

        return services

    def get_client_info(self, node_id: int) -> list[dict[str, Any]]:
        """Return enriched info for client ports: type name and possible server nodes."""
        node = self.all_nodes.get(node_id)
        if not node or not node.has_appeared or not node.has_clients:
            return []

        type_map = self.node_service_types.get(node_id, {})
        results = []
        for cid in node.client_ServiceIDs:
            cid_int = int(cid)
            type_str = type_map.get(cid_int)
            server_nodes = []
            for nid, n in self.all_nodes.items():
                if nid == node_id or not n.has_appeared or n.has_disappeared:
                    continue
                if n.has_servers and cid_int in [int(s) for s in n.server_ServiceIDs]:
                    server_nodes.append(nid)
            results.append({
                "service_id": cid_int,
                "full_type": type_str,
                "server_nodes": sorted(server_nodes),
            })
        return results

    def _describe_field(self, attr) -> dict[str, Any]:
        """Build a JSON-safe field descriptor from a pydsdl attribute."""
        type_str = str(attr.data_type)
        is_composite = isinstance(attr.data_type, CompositeType)
        desc = {
            "name": attr.name,
            "type": type_str,
            "kind": "composite" if is_composite else "primitive",
        }
        if is_composite:
            try:
                sub_fields = []
                for sub_attr in attr.data_type.attributes:
                    sub_fields.append({
                        "name": sub_attr.name,
                        "type": str(sub_attr.data_type),
                        "kind": "composite" if isinstance(sub_attr.data_type, CompositeType) else "primitive",
                    })
                desc["fields"] = sub_fields
            except Exception:
                desc["fields"] = []
        return desc
