[[ -z "$DOCKER_SUFFIX" ]] && { echo "set DOCKER_SUFFIX also in this file. Thx." >&2; exit 1; }

docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py createsuperuser
# docker exec toothfairy4m-db-$DOCKER_SUFFIX sh -lc 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -D "$MYSQL_DATABASE" -e "UPDATE scans_userprofile p JOIN auth_user u ON p.user_id=u.id SET p.role=\"admin\" WHERE u.username=\"llumetti\"; UPDATE auth_user SET is_staff=1 WHERE username=\"llumetti\";"'