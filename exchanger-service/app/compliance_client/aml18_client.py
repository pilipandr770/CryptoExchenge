"""Thin REST client for the AML-18 compliance-service. This service talks to
AML-18 as an external developer-portal integrator, exactly like any other
third-party project would (see AML-18's README "three entry points" table)
-- register once via AML-18's POST /developer/signup, then authenticate
every call with the issued Bearer API key (AML18_API_KEY).
"""

import requests


class Aml18ClientError(Exception):
    pass


class Aml18Client:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                timeout=self.timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise Aml18ClientError(f"AML-18 {method} {path} request failed: {exc}") from exc

        if response.status_code >= 400:
            raise Aml18ClientError(f"AML-18 {method} {path} failed ({response.status_code}): {response.text}")
        return response.json()

    def check_name(self, name: str, date_of_birth: str = None, country: str = None) -> dict:
        body = {"name": name}
        if date_of_birth:
            body["date_of_birth"] = date_of_birth
        if country:
            body["country"] = country
        return self._request("POST", "/screening/check-name", json=body)

    def wallet_ownership_requirement(self, transfer_amount_eur: float) -> dict:
        return self._request(
            "GET",
            "/wallet-ownership/requirement",
            params={"transfer_amount_eur": transfer_amount_eur},
        )

    def create_wallet_ownership_challenge(self, network: str, address: str) -> dict:
        return self._request(
            "POST",
            "/wallet-ownership/challenges",
            json={"network": network, "address": address},
        )

    def verify_wallet_ownership_signed_message(
        self,
        challenge_id: str,
        signature: str,
        transfer_amount_eur: float = None,
        transaction_id: str = None,
    ) -> dict:
        body = {"method": "signed_message", "challenge_id": challenge_id, "signature": signature}
        if transfer_amount_eur is not None:
            body["transfer_amount_eur"] = transfer_amount_eur
        if transaction_id is not None:
            body["transaction_id"] = transaction_id
        return self._request("POST", "/wallet-ownership/verifications", json=body)

    def get_wallet_ownership_verification(self, verification_id: str) -> dict:
        return self._request("GET", f"/wallet-ownership/verifications/{verification_id}")
