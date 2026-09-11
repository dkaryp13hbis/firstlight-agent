drop table if exists contracts;
drop index if exists organizations_vat_unique;
alter table organizations drop column if exists contact_phone;
alter table organizations drop column if exists contact_name;
alter table organizations drop column if exists country;
alter table organizations drop column if exists vat_number;
alter table organizations drop column if exists legal_name;
