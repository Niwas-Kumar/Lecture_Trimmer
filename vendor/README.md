# Supplied integration kit

`pw_access.py` is the supplied PW App Kit. Keep its `Lecture_Trimmer` application
name when replacing it with an updated kit. Our integration code lives in
`lecture_trimmer/integrations/pw_proxy.py`, which creates a separate kit instance
for each login. The kit's implementation is unchanged by source organization.
