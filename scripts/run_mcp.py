from app.mcp_server import server


if __name__ == "__main__":
    server.run(transport="streamable-http")
