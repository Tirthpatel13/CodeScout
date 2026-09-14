export interface Credentials {
  email: string;
  token: string;
}

export class ApiClient {
  constructor(private baseUrl: string, private token: string) {}

  async fetchUser(id: string): Promise<unknown> {
    return this.request(`/users/${id}`);
  }

  private async request(path: string): Promise<unknown> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      headers: { Authorization: `Bearer ${this.token}` },
    });
    return res.json();
  }
}

export function buildClient(creds: Credentials): ApiClient {
  return new ApiClient("https://api.example.com", creds.token);
}
