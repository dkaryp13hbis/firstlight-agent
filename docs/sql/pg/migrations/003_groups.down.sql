drop index if exists organizations_group_idx;
alter table organizations drop column if exists group_id;
drop table if exists groups;
