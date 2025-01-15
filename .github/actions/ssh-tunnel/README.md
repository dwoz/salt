
SSH Tunnel Action
=================

This action will create a reverse ssh tunnel to a github runner.


Usage
-----

Create a key for the server.

ssh-keygen -f ./runner -N ''

Copy the contents of the runner private key into a github secret which can ba accessed by your workflow.

```
steps:
  - name: SSH Tunnel
    uses: ./.github/actions/ssh-tunnel
    with:
      user: dan
      host: 95.212.213.150
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAVGQIPTuC5Hgj9h5LV5tda6nZdHCsFvqFjBvSAYjfEQ dan@carbon"
      private_key: "${{ secrets.PRIVATE_KEY }}"
```

