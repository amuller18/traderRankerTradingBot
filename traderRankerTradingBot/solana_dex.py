import requests
import json

class SolanaDex:
    @staticmethod
    def get_token_info(ca):
        """
        Fetches token information for a Solana contract address.
        """
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
            response = requests.get(url)
            if response.status_code != 200:
                print(f"Error fetching data from Dexscreener: {response.status_code}")
                return None

            data = response.json()
            pairs = data.get("pairs", [])
            if not pairs:
                print(f"No trading pairs found for address: {ca}")
                return None

            pair = pairs[0]
            return {
                "market_cap": pair.get("fdv", "N/A"),
                "price": pair.get("priceUsd", "N/A"),
                "name": pair.get("baseToken", {}).get("name", "Unknown"),
                "symbol": pair.get("baseToken", {}).get("symbol", "N/A"),
                "mint_address" : pair.get("pairAddress"),
                'supply': pair.get("baseToken", {}).get("totalSupply", "N/A")
            }

        except Exception as e:
            print(f"Error fetching token info: {e}")
            return None

    @staticmethod
    def get_token_supply(ca):
        url = "https://api.mainnet-beta.solana.com"

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [ca]
        }

        headers = {
            "Content-Type": "application/json"
        }

        response = requests.post(url, headers=headers, data=json.dumps(payload))

        if response.status_code == 200:
            data = response.json()
            print(data)
            return(data['result']['value']['decimals'])
        else:
            print(f"Error: {response.status_code}, {response.text}")
